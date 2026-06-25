//! URL canonicalization engine — primitives, pipeline, and probe.
//!
//! Functional core: [`engine`] (pure rule language + execution) and
//! [`url_model`] (ordered-query URL wrapper). Imperative shell: [`probe`]
//! (network) and `main.rs` (CLI).

pub mod engine;
pub mod probe;
pub mod rules;
pub mod url_model;
