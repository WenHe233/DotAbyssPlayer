// DotAbyss desktop shell (Tauri v2).
//
// Responsibilities:
//   * pick a free localhost port and spawn the Python backend sidecar (packaged exe, or
//     `python scripts/serve_advplayer.py` in dev), passing it the data dir + tool paths;
//   * wait for the backend's /healthz, then navigate the WebView to it (a spinner shows
//     until then);
//   * system tray (show / quit) + kill the backend child on exit;
//   * an `enable_long_paths` command the first-run wizard can call (elevated reg write).

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent, WebviewWindow};

struct Backend(Mutex<Option<Child>>);

/// Bind :0 to grab a free port from the OS, then release it for the child to reuse.
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8777)
}

/// Resolve the backend command: bundled PyInstaller exe if present, else dev Python.
fn backend_command(app: &tauri::AppHandle) -> Command {
    // Packaged: <resources>/backend/DotAbyssBackend.exe
    if let Ok(res) = app.path().resource_dir() {
        let exe = res.join("backend").join(if cfg!(windows) {
            "DotAbyssBackend.exe"
        } else {
            "DotAbyssBackend"
        });
        if exe.exists() {
            return Command::new(exe);
        }
    }
    // Dev fallback: run the source server with the repo's venv/python.
    let repo = repo_root();
    let py = repo.join(".venv").join(if cfg!(windows) {
        "Scripts/python.exe"
    } else {
        "bin/python"
    });
    let program = if py.exists() {
        py
    } else {
        PathBuf::from(if cfg!(windows) { "python.exe" } else { "python3" })
    };
    let mut cmd = Command::new(program);
    cmd.arg(repo.join("scripts").join("serve_advplayer.py"));
    cmd
}

/// Repo root for dev (compile-time desktop/src-tauri -> ../..).
fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Data dir: portable (data-store next to exe) if a `portable.txt` marker exists, else None
/// (the backend resolves %LOCALAPPDATA% itself).
fn portable_data_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    if dir.join("portable.txt").exists() {
        return Some(dir.join("data-store"));
    }
    None
}

fn spawn_backend(app: &tauri::AppHandle, port: u16) -> std::io::Result<Child> {
    let mut cmd = backend_command(app);
    cmd.arg("--host").arg("127.0.0.1").arg("--port").arg(port.to_string());

    if let Some(data) = portable_data_dir() {
        cmd.env("DOTABYSS_DATA_DIR", data);
    }
    // Point the backend at bundled tools if present (packaged build).
    if let Ok(res) = app.path().resource_dir() {
        let dl = res.join("bin").join(if cfg!(windows) {
            "DotAbyssClient.exe"
        } else {
            "DotAbyssClient"
        });
        if dl.exists() {
            cmd.env("DOTABYSS_DOWNLOADER", dl);
        }
        let vg = res.join("bin").join("vgmstream").join("vgmstream-cli.exe");
        if vg.exists() {
            cmd.env("DOTABYSS_VGMSTREAM", vg);
        }
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    cmd.spawn()
}

/// Minimal HTTP GET /healthz over a raw socket (avoids pulling in an HTTP client crate).
fn healthz_ok(port: u16) -> bool {
    let addr = format!("127.0.0.1:{port}");
    let Ok(mut stream) = TcpStream::connect_timeout(
        &addr.parse().unwrap(),
        Duration::from_millis(400),
    ) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
    let req = format!("GET /healthz HTTP/1.0\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = String::new();
    let _ = stream.read_to_string(&mut buf);
    buf.starts_with("HTTP/1.") && buf.contains(" 200")
}

fn navigate_when_ready(window: WebviewWindow, port: u16) {
    std::thread::spawn(move || {
        // Poll up to ~60s for the backend to come up.
        for _ in 0..150 {
            if healthz_ok(port) {
                if let Ok(url) = format!("http://127.0.0.1:{port}/").parse() {
                    let _ = window.navigate(url);
                }
                return;
            }
            std::thread::sleep(Duration::from_millis(400));
        }
        let _ = window.eval(
            "document.getElementById('msg') && (document.getElementById('msg').textContent='本地服务启动失败，请重启应用');",
        );
    });
}

/// Enable Windows long-path support (needs elevation). Returns Ok when the elevated
/// process was launched; the user still confirms the UAC prompt.
#[tauri::command]
fn enable_long_paths() -> Result<(), String> {
    #[cfg(windows)]
    {
        let ps = "Start-Process reg -ArgumentList 'add HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f' -Verb RunAs";
        Command::new("powershell")
            .args(["-NoProfile", "-Command", ps])
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        Ok(())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![enable_long_paths])
        .setup(|app| {
            let handle = app.handle().clone();
            let port = free_port();

            match spawn_backend(&handle, port) {
                Ok(child) => {
                    app.state::<Backend>().0.lock().unwrap().replace(child);
                }
                Err(e) => {
                    eprintln!("failed to spawn backend: {e}");
                }
            }

            build_tray(&handle)?;

            if let Some(window) = app.get_webview_window("main") {
                navigate_when_ready(window, port);
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<Backend>() {
                    if let Some(mut child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}

fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::TrayIconBuilder;

    let show = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    TrayIconBuilder::new()
        .icon(app.default_window_icon().unwrap().clone())
        .tooltip("ドットアビス 剧情播放器")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}
