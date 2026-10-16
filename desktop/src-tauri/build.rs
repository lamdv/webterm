fn main() {
    // Declaring the app's commands in the ACL manifest is what makes the
    // capability in `capabilities/default.json` meaningful: Tauri only enforces
    // per-command permissions once the app has its own manifest.  Without this,
    // custom commands are reachable from *any* window, including the remote
    // pages loaded in server windows.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "list_profiles",
            "add_profile",
            "update_profile",
            "remove_profile",
            "connect",
            "probe_all",
        ]),
    ))
    .expect("failed to run tauri-build");
}
