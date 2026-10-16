use pyo3::prelude::*;

pub fn tauri_generate_context() -> tauri::Context {
    tauri::generate_context!()
}

#[pymodule(gil_used = false)]
#[pyo3(name = "ext_mod")]
pub mod ext_mod {
    use super::*;

    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        pytauri::pymodule_export(
            module,
            // `context_factory`: the webterm_desktop Python package calls this
            // when bundled, instead of reading Tauri.toml from the source tree.
            |_args, _kwargs| Ok(tauri_generate_context()),
            // `builder_factory`: Python adds setup, plugins and the IPC handler.
            |_args, _kwargs| Ok(tauri::Builder::default()),
        )
    }
}
