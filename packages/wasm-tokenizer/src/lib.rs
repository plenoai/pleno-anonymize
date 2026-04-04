use lindera::segmenter::Segmenter;
use lindera::tokenizer::Tokenizer;
use serde::Serialize;
use wasm_bindgen::prelude::*;

/// murmurhash2_64a (seed=1) — spaCy hash_string 互換
fn murmurhash2_64a(data: &[u8]) -> u64 {
    const M: u64 = 0xc6a4a7935bd1e995;
    const R: u32 = 47;
    let seed: u64 = 1;
    let len = data.len() as u64;
    let mut h: u64 = seed ^ len.wrapping_mul(M);

    let n_blocks = data.len() / 8;
    for i in 0..n_blocks {
        let offset = i * 8;
        let k = u64::from_le_bytes([
            data[offset],
            data[offset + 1],
            data[offset + 2],
            data[offset + 3],
            data[offset + 4],
            data[offset + 5],
            data[offset + 6],
            data[offset + 7],
        ]);
        let mut k = k.wrapping_mul(M);
        k ^= k >> R;
        k = k.wrapping_mul(M);
        h ^= k;
        h = h.wrapping_mul(M);
    }

    let tail = n_blocks * 8;
    let remaining = data.len() & 7;
    for i in (0..remaining).rev() {
        h ^= (data[tail + i] as u64) << (i * 8);
    }
    if remaining > 0 {
        h = h.wrapping_mul(M);
    }

    h ^= h >> R;
    h = h.wrapping_mul(M);
    h ^= h >> R;

    h
}

fn hash_string(s: &str) -> u64 {
    murmurhash2_64a(s.as_bytes())
}

/// 文字種パターン (spaCy SHAPE互換)
fn compute_shape(text: &str) -> String {
    let mut shape = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '\u{4E00}'..='\u{9FFF}' | '\u{3400}'..='\u{4DBF}' => shape.push('x'),
            '\u{3040}'..='\u{309F}' => shape.push('x'),
            '\u{30A0}'..='\u{30FF}' | '\u{FF65}'..='\u{FF9F}' => shape.push('X'),
            'A'..='Z' | '\u{FF21}'..='\u{FF3A}' => shape.push('X'),
            'a'..='z' | '\u{FF41}'..='\u{FF5A}' => shape.push('x'),
            '0'..='9' | '\u{FF10}'..='\u{FF19}' => shape.push('d'),
            other => shape.push(other),
        }
    }
    shape
}

/// 全角→半角正規化 + 小文字化 (spaCy NORM互換)
fn normalize(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        let c = match ch {
            '\u{FF10}'..='\u{FF5A}' => {
                let code = ch as u32 - 0xFEE0;
                char::from_u32(code).unwrap_or(ch)
            }
            other => other,
        };
        for lc in c.to_lowercase() {
            out.push(lc);
        }
    }
    out
}

#[derive(Serialize)]
pub struct TokenResult {
    pub text: String,
    pub start: usize,
    pub end: usize,
    /// [norm_lo, prefix_lo, suffix_lo, shape_lo]
    pub hashes_lo: [u32; 4],
    /// [norm_hi, prefix_hi, suffix_hi, shape_hi]
    pub hashes_hi: [u32; 4],
}

#[wasm_bindgen]
pub struct JaTokenizer {
    tokenizer: Tokenizer,
}

fn create_tokenizer() -> Result<Tokenizer, Box<dyn std::error::Error>> {
    let dictionary = lindera::dictionary::load_dictionary("embedded://ipadic")?;
    let segmenter = Segmenter::new(
        lindera::mode::Mode::Normal,
        dictionary,
        None,
    );
    Ok(Tokenizer::new(segmenter))
}

#[wasm_bindgen]
impl JaTokenizer {
    #[wasm_bindgen(constructor)]
    pub fn new() -> Result<JaTokenizer, JsError> {
        let tokenizer = create_tokenizer()
            .map_err(|e| JsError::new(&format!("Failed to create tokenizer: {}", e)))?;
        Ok(JaTokenizer { tokenizer })
    }

    /// テキストをトークン化し、各トークンのハッシュ特徴量を返す。
    pub fn tokenize(&self, text: &str) -> Result<JsValue, JsError> {
        let mut tokens = self
            .tokenizer
            .tokenize(text)
            .map_err(|e| JsError::new(&format!("Tokenize failed: {}", e)))?;

        let mut results: Vec<TokenResult> = Vec::with_capacity(tokens.len());

        for token in tokens.iter_mut() {
            let tok_text = token.surface.to_string();
            let byte_start = token.byte_start;
            let byte_end = token.byte_end;

            // byte offset → char offset
            let char_start = text[..byte_start].chars().count();
            let char_end = text[..byte_end].chars().count();

            // NORM: 原形（lemma）があればそれを使用、なければ表層形
            let lemma = token.get_detail(6)
                .filter(|s| *s != "*")
                .map(|s| s.to_string());
            let norm_text = lemma.unwrap_or_else(|| tok_text.clone());
            let norm = normalize(&norm_text);
            let chars: Vec<char> = norm.chars().collect();
            let prefix = chars.first().map(|c| c.to_string()).unwrap_or_default();
            let suffix = if chars.len() <= 3 {
                norm.clone()
            } else {
                chars[chars.len() - 3..].iter().collect()
            };
            let shape = compute_shape(&tok_text);

            let h_norm = hash_string(&norm);
            let h_prefix = hash_string(&prefix);
            let h_suffix = hash_string(&suffix);
            let h_shape = hash_string(&shape);

            results.push(TokenResult {
                text: tok_text,
                start: char_start,
                end: char_end,
                hashes_lo: [
                    h_norm as u32,
                    h_prefix as u32,
                    h_suffix as u32,
                    h_shape as u32,
                ],
                hashes_hi: [
                    (h_norm >> 32) as u32,
                    (h_prefix >> 32) as u32,
                    (h_suffix >> 32) as u32,
                    (h_shape >> 32) as u32,
                ],
            });
        }

        serde_wasm_bindgen::to_value(&results).map_err(|e| JsError::new(&e.to_string()))
    }
}
