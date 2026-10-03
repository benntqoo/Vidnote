//! 定位 Python worker 的启动目标（程序、参数、工作目录）。
//!
//! 开发期（`tauri dev`）与打包后（sidecar）的形态完全不同，这里统一收口。
//! 定位失败时要能说清「是哪条规则都没命中」，而不是抛一句 path not found。

use std::path::{Path, PathBuf};

/// worker 的启动目标。
#[derive(Debug, Clone)]
pub struct WorkerTarget {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    /// 命中这条规则的说明。出错诊断时直接展示给用户。
    pub source: String,
}

/// 仓库根目录。`CARGO_MANIFEST_DIR` = `<repo>/app-ui/src-tauri`，
/// 因此上溯两级即 `<repo>`。编译期常量，运行时零成本。
pub fn project_root() -> Option<PathBuf> {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .map(Path::to_path_buf)
}

/// 定位 worker 启动目标。
///
/// 优先级（**开发模式**）：
/// 1. `VIDNOTE_PYTHON` 环境变量 —— 显式指定，最高优先级。本项目依赖装在受管 venv 里，
///    不在项目目录内，所以这条是日常开发的主力路径。
/// 2. `<repo>/.venv/Scripts/python.exe`（Windows）/ `<repo>/.venv/bin/python`（Unix）
/// 3. PATH 中的 `python`
///
/// **打包模式**：用可执行文件同目录下的 `vidnote-worker.exe`（Tauri sidecar 产物，阶段 5）。
pub fn locate() -> Result<WorkerTarget, String> {
    if cfg!(debug_assertions) {
        locate_dev()
    } else {
        locate_bundled()
    }
}

fn locate_dev() -> Result<WorkerTarget, String> {
    let root = project_root().ok_or_else(|| {
        format!(
            "无法从 CARGO_MANIFEST_DIR({}) 上溯定位仓库根",
            env!("CARGO_MANIFEST_DIR")
        )
    })?;

    // 1. 显式环境变量
    if let Ok(p) = std::env::var("VIDNOTE_PYTHON") {
        let path = PathBuf::from(&p);
        if path.is_file() {
            return Ok(WorkerTarget {
                program: path,
                args: module_args(),
                cwd: root,
                source: format!("环境变量 VIDNOTE_PYTHON = {p}"),
            });
        }
        return Err(format!(
            "环境变量 VIDNOTE_PYTHON 指向的文件不存在：{p}\n\
             本项目依赖（faster-whisper / ctranslate2）不在系统 Python 里，必须指向装有依赖的解释器。"
        ));
    }

    // 2. 项目内虚拟环境
    let candidates: [PathBuf; 2] = [
        root.join(".venv").join("Scripts").join("python.exe"),
        root.join(".venv").join("bin").join("python"),
    ];
    for cand in candidates.iter() {
        if cand.is_file() {
            return Ok(WorkerTarget {
                program: cand.clone(),
                args: module_args(),
                cwd: root,
                source: format!("项目虚拟环境 {}", cand.display()),
            });
        }
    }

    // 3. PATH
    if let Some(found) = which("python") {
        return Ok(WorkerTarget {
            program: found.clone(),
            args: module_args(),
            cwd: root,
            source: format!("PATH 中的 python（{}）—— 注意：该解释器可能未安装项目依赖", found.display()),
        });
    }

    Err(format!(
        "找不到 Python 解释器。已尝试：\n\
         1. 环境变量 VIDNOTE_PYTHON\n\
         2. {}\n\
         3. PATH 中的 python\n\
         请设置 VIDNOTE_PYTHON 指向装有 faster-whisper 的解释器。",
        root.join(".venv").join("Scripts").join("python.exe").display()
    ))
}

fn locate_bundled() -> Result<WorkerTarget, String> {
    let exe = std::env::current_exe()
        .map_err(|e| format!("取 current_exe 失败：{e}"))?;
    let dir = exe
        .parent()
        .ok_or_else(|| "可执行文件没有父目录".to_string())?;

    let name = if cfg!(windows) {
        "vidnote-worker.exe"
    } else {
        "vidnote-worker"
    };
    let program = dir.join(name);
    if !program.is_file() {
        return Err(format!(
            "打包模式找不到 sidecar：{}\n\
             （阶段 5 的 PyInstaller 产物，需配置为 tauri.conf.json 的 bundle.externalBin）",
            program.display()
        ));
    }

    Ok(WorkerTarget {
        program,
        args: Vec::new(), // 打包后是独立可执行文件，不需要 `-m app.rpc`
        cwd: dir.to_path_buf(),
        source: "同目录 sidecar".to_string(),
    })
}

/// 开发模式以模块方式启动：`python -m app.rpc`（cwd 必须是仓库根，否则 import 不到 app 包）。
fn module_args() -> Vec<String> {
    vec!["-m".to_string(), "app.rpc".to_string()]
}

/// 在 PATH 中查找可执行文件。自己实现是为了不引入 `which` crate —— 逻辑只有十几行。
fn which(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    let exts: Vec<String> = if cfg!(windows) {
        std::env::var("PATHEXT")
            .unwrap_or_else(|_| ".EXE;.CMD;.BAT".to_string())
            .split(';')
            .map(|s| s.to_ascii_lowercase())
            .collect()
    } else {
        vec![String::new()]
    };

    for dir in std::env::split_paths(&path_var) {
        for ext in &exts {
            let cand = dir.join(format!("{name}{ext}"));
            if cand.is_file() {
                return Some(cand);
            }
            // Windows 上 PATH 里的名字大小写不定，再试一次大写
            if cfg!(windows) {
                let upper = dir.join(format!("{}{}", name.to_uppercase(), ext.to_uppercase()));
                if upper.is_file() {
                    return Some(upper);
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn project_root_contains_expected_markers() {
        let root = project_root().expect("应能定位仓库根");
        // 用两个独立标记交叉验证，避免上溯级数写错还能「看起来成功」
        assert!(root.join("PLAN.md").is_file(), "缺 PLAN.md：{}", root.display());
        assert!(root.join("app").join("rpc.py").is_file(), "缺 app/rpc.py：{}", root.display());
    }

    #[test]
    fn module_args_point_to_rpc_entry() {
        assert_eq!(module_args(), vec!["-m".to_string(), "app.rpc".to_string()]);
    }
}
