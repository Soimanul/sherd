//! Streaming WhatsApp text parser. Python owns timezones and canonical identities.
mod patterns;

use chrono::NaiveDate;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use regex::Regex;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Read};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::LazyLock;

type WallTime = (i32, u32, u32, u32, u32, u32);
type Record = (
    Option<WallTime>,
    Option<String>,
    String,
    String,
    Option<String>,
    Option<String>,
    bool,
);
static SYSTEM: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(patterns::SYSTEM).expect("static system regex"));
static GROUP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(patterns::GROUP).expect("static group regex"));
static MEDIA: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(patterns::MEDIA).expect("static media regex"));

fn protected<T>(f: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    catch_unwind(AssertUnwindSafe(f))
        .unwrap_or_else(|_| Err(PyRuntimeError::new_err("WhatsApp parser failed")))
}

struct PythonReader(Py<PyAny>);
impl Read for PythonReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        Python::attach(|py| {
            let result = self
                .0
                .call_method1(py, "read", (buf.len(),))
                .and_then(|v| v.extract::<Vec<u8>>(py))
                .map_err(|e| io::Error::other(e.to_string()))?;
            if result.len() > buf.len() {
                return Err(io::Error::other(
                    "stream returned more bytes than requested",
                ));
            }
            buf[..result.len()].copy_from_slice(&result);
            Ok(result.len())
        })
    }
}

struct Lines {
    reader: BufReader<Box<dyn Read + Send + Sync>>,
    after_cr: bool,
}
impl Lines {
    fn open(source: &Bound<'_, PyAny>) -> PyResult<Self> {
        let reader: Box<dyn Read + Send + Sync> = if let Ok(path) = source.extract::<String>() {
            Box::new(File::open(path).map_err(|e| PyOSError::new_err(e.to_string()))?)
        } else {
            Box::new(PythonReader(source.clone().unbind()))
        };
        Ok(Self {
            reader: BufReader::with_capacity(65536, reader),
            after_cr: false,
        })
    }
    // Universal newlines, including CRLF split across chunk boundaries.
    fn next(&mut self) -> io::Result<Option<String>> {
        let mut bytes = Vec::new();
        loop {
            let buf = self.reader.fill_buf()?;
            if buf.is_empty() {
                return Ok(
                    (!bytes.is_empty()).then(|| String::from_utf8_lossy(&bytes).into_owned())
                );
            }
            if self.after_cr {
                self.after_cr = false;
                if buf[0] == b'\n' {
                    self.reader.consume(1);
                    continue;
                }
            }
            let buf = self.reader.fill_buf()?;
            if let Some(end) = memchr::memchr2(b'\n', b'\r', buf) {
                bytes.extend_from_slice(&buf[..end]);
                self.after_cr = buf[end] == b'\r';
                self.reader.consume(end + 1);
                return Ok(Some(String::from_utf8_lossy(&bytes).into_owned()));
            }
            let size = buf.len();
            bytes.extend_from_slice(buf);
            self.reader.consume(size);
        }
    }
}
fn mark(c: char) -> bool {
    matches!(c, '\u{200e}' | '\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
}
fn clean(s: &str) -> String {
    if memchr::memchr(0xe2, s.as_bytes()).is_some() && s.chars().any(mark) {
        s.chars().filter(|c| !mark(*c)).collect()
    } else {
        s.to_owned()
    }
}
fn leading(s: &str) -> &str {
    s.trim_start_matches(|c| c == '\u{feff}' || mark(c))
}

#[derive(Clone, Debug)]
struct Prefix {
    a: u32,
    b: u32,
    year: i32,
    h: u32,
    m: u32,
    s: u32,
    ap: Option<bool>,
}
impl Prefix {
    fn wall(&self, md: bool) -> Option<WallTime> {
        let (month, day) = if md {
            (self.a, self.b)
        } else {
            (self.b, self.a)
        };
        let h = if let Some(pm) = self.ap {
            if !(1..=12).contains(&self.h) {
                return None;
            }
            self.h % 12 + if pm { 12 } else { 0 }
        } else {
            self.h
        };
        NaiveDate::from_ymd_opt(self.year, month, day)?.and_hms_opt(h, self.m, self.s)?;
        // Python datetime supports years 1..9999 only.
        (self.year <= 9999).then_some((self.year, month, day, h, self.m, self.s))
    }
}
// Unicode 15 decimal ranges match Python 3.12's \d/int behavior; ASCII is the hot path.
fn digit(c: char) -> Option<u32> {
    if c.is_ascii_digit() {
        return Some(c as u32 - '0' as u32);
    }
    const STARTS: &[u32] = &[
        0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66, 0xde6,
        0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60,
        0x16ac0, 0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0,
        0x1e950, 0x1fbf0,
    ];
    let n = c as u32;
    STARTS
        .iter()
        .find_map(|start| (n >= *start && n < *start + 10).then(|| n - start))
}
fn number(s: &str, pos: &mut usize, min: usize, max: usize) -> Option<u32> {
    let mut count = 0;
    let mut n = 0;
    while count < max {
        let Some(c) = s[*pos..].chars().next() else {
            break;
        };
        let Some(d) = digit(c) else { break };
        n = n * 10 + d;
        *pos += c.len_utf8();
        count += 1;
    }
    (count >= min).then_some(n)
}
fn whitespace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}
fn byte(s: &str, pos: &mut usize, b: u8) -> Option<()> {
    if s.as_bytes().get(*pos) == Some(&b) {
        *pos += 1;
        Some(())
    } else {
        None
    }
}
fn spaces(s: &str, pos: &mut usize) {
    while let Some(c) = s[*pos..].chars().next() {
        if !whitespace(c) {
            break;
        }
        *pos += c.len_utf8();
    }
}
fn prefix(s: &str) -> Option<(Prefix, &str)> {
    let mut p = 0;
    let ios = s.starts_with('[');
    if ios {
        p += 1;
    }
    let a = number(s, &mut p, 1, 2)?;
    if !matches!(s.as_bytes().get(p), Some(b'/' | b'.')) {
        return None;
    }
    p += 1;
    let b = number(s, &mut p, 1, 2)?;
    if !matches!(s.as_bytes().get(p), Some(b'/' | b'.')) {
        return None;
    }
    p += 1;
    let start = p;
    let y = number(s, &mut p, 2, 4)?;
    if !matches!(s[start..p].chars().count(), 2 | 4) {
        return None;
    }
    byte(s, &mut p, b',')?;
    spaces(s, &mut p);
    let h = number(s, &mut p, 1, 2)?;
    byte(s, &mut p, b':')?;
    let m = number(s, &mut p, 2, 2)?;
    let sec = if s.as_bytes().get(p) == Some(&b':') {
        p += 1;
        number(s, &mut p, 2, 2)?
    } else {
        0
    };
    let before = p;
    spaces(s, &mut p);
    let mut ap = None;
    if matches!(s.as_bytes().get(p), Some(b'a' | b'A' | b'p' | b'P')) {
        let pm = matches!(s.as_bytes()[p], b'p' | b'P');
        p += 1;
        if s.as_bytes().get(p) == Some(&b'.') {
            p += 1;
        }
        if let Some(c) = s[p..].chars().next()
            && whitespace(c)
        {
            p += c.len_utf8();
        }
        if !matches!(s.as_bytes().get(p), Some(b'm' | b'M')) {
            return None;
        }
        p += 1;
        if s.as_bytes().get(p) == Some(&b'.') {
            p += 1;
        }
        ap = Some(pm);
    } else {
        p = before;
    }
    if ios {
        byte(s, &mut p, b']')?;
        spaces(s, &mut p);
    } else {
        let before = p;
        spaces(s, &mut p);
        if p == before {
            return None;
        }
        byte(s, &mut p, b'-')?;
        let before = p;
        spaces(s, &mut p);
        if p == before {
            return None;
        }
    }
    Some((
        Prefix {
            a,
            b,
            year: if y < 100 { y as i32 + 2000 } else { y as i32 },
            h,
            m,
            s: sec,
            ap,
        },
        &s[p..],
    ))
}
fn split(body: &str) -> (Option<String>, String) {
    let parts = body
        .split_once(": ")
        .or_else(|| body.strip_suffix(':').map(|s| (s, "")));
    match parts {
        None => (None, clean(body)),
        Some((sender, text)) => {
            let text_clean = clean(text);
            if text.starts_with('\u{200e}') && SYSTEM.is_match(text_clean.trim_matches(whitespace))
            {
                (None, text_clean)
            } else {
                (
                    Some(clean(sender).trim_matches(whitespace).to_owned()),
                    text_clean,
                )
            }
        }
    }
}
fn classify(sender: &Option<String>, body: &str) -> (String, Option<String>, Option<String>) {
    let text = (!body.is_empty()).then(|| body.to_owned());
    if sender.is_none() {
        return ("system".into(), text, None);
    }
    let lower = if body.trim_matches(whitespace).len() <= 100 {
        body.trim_matches(whitespace)
            .strip_suffix('.')
            .unwrap_or(body.trim_matches(whitespace))
            .to_lowercase()
    } else {
        String::new()
    };
    if matches!(
        lower.as_str(),
        "this message was deleted"
            | "you deleted this message"
            | "acest mesaj a fost șters"
            | "ai șters acest mesaj"
            | "dieser nachricht wurde gelöscht"
            | "diese nachricht wurde gelöscht"
            | "du hast diese nachricht gelöscht"
            | "este mensaje fue eliminado"
            | "eliminaste este mensaje"
            | "borraste este mensaje"
    ) {
        return ("deleted".into(), None, None);
    }
    // Almost all exported rows are ordinary text; avoid regex work on that path.
    if (body.starts_with('<')
        || body
            .as_bytes()
            .windows(7)
            .any(|w| w.eq_ignore_ascii_case(b"omitted"))
        || body
            .as_bytes()
            .windows(15)
            .any(|w| w.eq_ignore_ascii_case(b"(file attached)")))
        && let Some(c) = MEDIA.captures(body)
    {
        let caption = c.name("caption").map_or("", |m| m.as_str());
        if !caption.is_empty() && !caption.starts_with(whitespace) {
            return ("text".into(), text, None);
        }
        let explicit = c.name("type").map(|v| v.as_str().to_lowercase());
        let file = c
            .name("ios")
            .or_else(|| c.name("android"))
            .map_or("", |v| v.as_str())
            .to_lowercase();
        let ext = std::path::Path::new(&file)
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("");
        let kind = explicit.unwrap_or_else(|| {
            if file.contains("sticker") || ext == "webp" {
                "sticker"
            } else if ext == "gif" {
                "gif"
            } else if matches!(ext, "jpg" | "jpeg" | "png" | "heic") {
                "image"
            } else if matches!(ext, "mp4" | "mov" | "3gp") {
                "video"
            } else if matches!(ext, "opus" | "ogg" | "mp3" | "m4a" | "wav") {
                "audio"
            } else if !ext.is_empty() {
                "document"
            } else {
                "other"
            }
            .to_owned()
        });
        return (
            "media".into(),
            (!caption.trim_matches(whitespace).is_empty())
                .then(|| caption.trim_matches(whitespace).to_owned()),
            Some(kind),
        );
    }
    ("text".into(), text, None)
}

#[derive(Default)]
struct DateHints {
    dm: bool,
    md: bool,
    twelve: bool,
}
impl DateHints {
    fn observe(&mut self, p: &Prefix) {
        if p.wall(false).is_some() || p.wall(true).is_some() {
            self.dm |= p.a > 12;
            self.md |= p.b > 12;
            self.twelve |= p.ap.is_some();
        }
    }
    fn order(&self) -> &'static str {
        if self.dm {
            "dm"
        } else if self.md || self.twelve {
            "md"
        } else {
            "dm"
        }
    }
}

#[pyfunction]
fn detect_date_order(source: &Bound<'_, PyAny>) -> PyResult<&'static str> {
    protected(|| {
        let mut lines = Lines::open(source)?;
        let mut hints = DateHints::default();
        while let Some(line) = lines
            .next()
            .map_err(|e| PyOSError::new_err(e.to_string()))?
        {
            if let Some((p, _)) = prefix(&clean(leading(&line))) {
                hints.observe(&p);
            }
        }
        Ok(hints.order())
    })
}

#[pyclass]
struct RecordIterator {
    lines: Lines,
    md: bool,
    prefixes: bool,
    pending: Option<(Prefix, Option<String>, String)>,
    done: bool,
}
impl RecordIterator {
    fn record(&self, p: Prefix, sender: Option<String>, original: String) -> Record {
        let group = sender.is_none() && GROUP.is_match(&original);
        let (kind, text, media) = if self.prefixes {
            ("text".into(), None, None)
        } else {
            classify(&sender, &original)
        };
        (p.wall(self.md), sender, original, kind, text, media, group)
    }
    fn next_record(&mut self) -> io::Result<Option<Record>> {
        if self.done {
            return Ok(None);
        }
        while let Some(line) = self.lines.next()? {
            let line = leading(&line);
            if let Some((p, body)) = prefix(line) {
                let (sender, text) = split(body);
                if self.prefixes {
                    return Ok(Some(self.record(p, sender, text)));
                }
                let previous = self.pending.replace((p, sender, text));
                if let Some((p, s, t)) = previous {
                    return Ok(Some(self.record(p, s, t)));
                }
            } else if !self.prefixes
                && let Some((_, _, text)) = &mut self.pending
            {
                text.push('\n');
                text.push_str(&clean(line));
            }
        }
        self.done = true;
        Ok(self.pending.take().map(|(p, s, t)| self.record(p, s, t)))
    }
}
#[pymethods]
impl RecordIterator {
    #[new]
    #[pyo3(signature = (source, order, *, prefixes=false))]
    fn new(source: &Bound<'_, PyAny>, order: &str, prefixes: bool) -> PyResult<Self> {
        protected(|| {
            if !matches!(order, "dm" | "md") {
                return Err(PyValueError::new_err("order must be dm or md"));
            }
            Ok(Self {
                lines: Lines::open(source)?,
                md: order == "md",
                prefixes,
                pending: None,
                done: false,
            })
        })
    }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __next__(&mut self) -> PyResult<Option<Vec<Record>>> {
        protected(|| {
            let mut batch = Vec::with_capacity(1024);
            let mut bytes = 0;
            for _ in 0..1024 {
                match self
                    .next_record()
                    .map_err(|e| PyOSError::new_err(e.to_string()))?
                {
                    Some(r) => {
                        bytes += r.2.len();
                        batch.push(r);
                        if bytes >= 1024 * 1024 {
                            break;
                        }
                    }
                    None => break,
                }
            }
            Ok((!batch.is_empty()).then_some(batch))
        })
    }
}
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RecordIterator>()?;
    m.add_function(wrap_pyfunction!(detect_date_order, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dates_and_clocks() {
        assert!(prefix("13/01/2024, 09:00- Mira Example: hi").is_none());
        let (p, _) = prefix("13/01/2024, 09:00 - Mira Example: hi").unwrap();
        assert!(p.wall(false).is_some());
        assert!(p.wall(true).is_none());
        let (p, _) = prefix("[1/13/24, 12:00:05 p. m.] hi").unwrap();
        assert_eq!(p.wall(true), Some((2024, 1, 13, 12, 0, 5)));
        assert!(
            prefix("[1/13/24, 00:00 PM] hi")
                .unwrap()
                .0
                .wall(true)
                .is_none()
        );
    }
    #[test]
    fn line_classification() {
        assert_eq!(
            split("Mira Example: \u{200e}Missed voice call."),
            (None, "Missed voice call.".into())
        );
        assert_eq!(
            split("Mira Example: I left.").0,
            Some("Mira Example".into())
        );
        assert_eq!(
            classify(&Some("Mira Example".into()), "<attached: IMG.jpg> caption").2,
            Some("image".into())
        );
        assert_eq!(
            classify(&Some("Mira Example".into()), "Acest mesaj a fost șters.").0,
            "deleted"
        );
    }
    #[test]
    fn group_notice_with_blank_continuation() {
        assert!(GROUP.is_match("Mira Example left\n"));
        assert!(!GROUP.is_match("Mira Example left\nmore"));
    }
    #[test]
    fn date_order_detection() {
        let mut hints = DateHints::default();
        assert_eq!(hints.order(), "dm");
        hints.observe(&prefix("1/2/24, 9:00 AM - hi").unwrap().0);
        assert_eq!(hints.order(), "md");
        hints.observe(&prefix("13/1/24, 09:00 - hi").unwrap().0);
        assert_eq!(hints.order(), "dm");
        hints.observe(&prefix("1/14/24, 09:00 - hi").unwrap().0);
        assert_eq!(hints.order(), "dm");
        let mut invalid = DateHints::default();
        invalid.observe(&prefix("1/14/24, 00:00 AM - hi").unwrap().0);
        assert_eq!(invalid.order(), "dm");
    }
    #[test]
    fn unicode_clock_and_invalid_dates() {
        let (p, _) = prefix("[١٣/٠١/٢٠٢٤, ٩:٠٠:٠٥] hi").unwrap();
        assert_eq!(p.wall(false), Some((2024, 1, 13, 9, 0, 5)));
        assert!(
            prefix("31/02/24, 09:00 - hi")
                .unwrap()
                .0
                .wall(false)
                .is_none()
        );
    }
}
