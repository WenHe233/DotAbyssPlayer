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
use std::process::{Child, Command, Stdio};
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

/// Look for a bundled resource across candidate roots: the Tauri resource dir (NSIS
/// install) and the exe's own directory (portable layout).
fn resource_file(app: &tauri::AppHandle, rel: &str) -> Option<PathBuf> {
    let mut roots: Vec<PathBuf> = Vec::new();
    if let Ok(r) = app.path().resource_dir() {
        roots.push(r);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            roots.push(dir.to_path_buf());
        }
    }
    roots.into_iter().map(|r| r.join(rel)).find(|p| p.exists())
}

/// Resolve the backend command: bundled PyInstaller exe if present, else dev Python.
fn backend_command(app: &tauri::AppHandle) -> Command {
    let backend_rel = if cfg!(windows) {
        "backend/DotAbyssBackend.exe"
    } else {
        "backend/DotAbyssBackend"
    };
    if let Some(exe) = resource_file(app, backend_rel) {
        return Command::new(exe);
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

/// Data dir: portable (`data-store` next to exe) when a `portable.txt` marker exists.
fn portable_data_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    if dir.join("portable.txt").exists() {
        return Some(dir.join("data-store"));
    }
    None
}

/// The data dir the shell agrees on with the backend (kept in sync so logs land beside data).
fn data_dir() -> PathBuf {
    if let Some(p) = portable_data_dir() {
        return p;
    }
    let base = std::env::var("LOCALAPPDATA")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| std::env::var("HOME").ok())
        .unwrap_or_else(|| ".".into());
    PathBuf::from(base).join("DotAbyssPlayer").join("data")
}

fn backend_log_path() -> PathBuf {
    data_dir().join("backend.log")
}

fn spawn_backend(app: &tauri::AppHandle, port: u16) -> std::io::Result<Child> {
    let data = data_dir();
    let _ = std::fs::create_dir_all(&data);

    let mut cmd = backend_command(app);
    cmd.arg("--host").arg("127.0.0.1").arg("--port").arg(port.to_string());
    cmd.env("DOTABYSS_DATA_DIR", &data);

    // Point the backend at bundled tools if present (packaged / portable build).
    let downloader_rel = if cfg!(windows) { "bin/DotAbyssClient.exe" } else { "bin/DotAbyssClient" };
    if let Some(dl) = resource_file(app, downloader_rel) {
        cmd.env("DOTABYSS_DOWNLOADER", dl);
    }
    if let Some(vg) = resource_file(app, "bin/vgmstream/vgmstream-cli.exe") {
        cmd.env("DOTABYSS_VGMSTREAM", vg);
    }

    // Redirect the child's stdout+stderr to a log file so import-time crashes / tracebacks
    // are visible even though the shell spawns it without a console. Append (not truncate) so a
    // later relaunch doesn't wipe the setup-phase errors that explain a broken install.
    if let Ok(out) = std::fs::OpenOptions::new().create(true).append(true).open(backend_log_path()) {
        if let Ok(err) = out.try_clone() {
            cmd.stdout(Stdio::from(out)).stderr(Stdio::from(err));
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

fn fail_message(window: &WebviewWindow, extra: &str) {
    let log = backend_log_path().to_string_lossy().replace('\\', "\\\\");
    let msg = format!(
        "本地服务启动失败{extra}。日志：{log}",
    );
    let js = format!(
        "document.getElementById('msg') && (document.getElementById('msg').textContent='{}');",
        msg.replace('\'', "\\'")
    );
    let _ = window.eval(&js);
}

fn backend_alive(app: &tauri::AppHandle) -> bool {
    if let Some(state) = app.try_state::<Backend>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.as_mut() {
                // try_wait -> Ok(Some(_)) means it already exited.
                return !matches!(child.try_wait(), Ok(Some(_)));
            }
        }
    }
    false
}

fn navigate_when_ready(app: tauri::AppHandle, window: WebviewWindow, port: u16) {
    std::thread::spawn(move || {
        // Poll up to ~60s for the backend to come up, but fail fast if it exits early.
        for _ in 0..150 {
            if healthz_ok(port) {
                if let Ok(url) = format!("http://127.0.0.1:{port}/").parse() {
                    let _ = window.navigate(url);
                }
                return;
            }
            if !backend_alive(&app) {
                fail_message(&window, "（后端进程已退出，多为缺依赖/资源）");
                return;
            }
            std::thread::sleep(Duration::from_millis(400));
        }
        fail_message(&window, "（超时）");
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
                navigate_when_ready(handle.clone(), window, port);
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
