// Prevents an extra console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// The binary is intentionally trivial: it hands straight to the library so that
// everything testable lives in a `lib` target (integration tests and mobile
// builds both need that).
fn main() {
    webterm_desktop_lib::run();
}
