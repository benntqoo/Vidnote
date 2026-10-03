//! Windows Job Object 封装。
//!
//! 目的：Tauri 应用（父进程）被强杀时，Python worker 随之终止，不留孤儿进程。
//! 规格 `docs/IPC协议规格.md` §7 要求设 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`。
//!
//! **注意防线顺序**：Job Object 是第二道防线。第一道是 Python 侧的 stdin EOF 自杀
//! （父进程消失 → 管道关闭 → worker 自行退出）—— 即使 Job Object 因权限问题失效，
//! 那道防线仍然成立。

#[cfg(windows)]
mod platform {
    use std::io;
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    /// 一个 Job Object 句柄。**Drop 即关闭句柄**——由于设了
    /// `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，关闭时作业内所有进程会被终止。
    /// 这正是我们想要的：supervisor 被 drop（进程退出路径）时连带杀掉 worker。
    pub struct JobObject(HANDLE);

    // HANDLE 本质是裸指针，但 Job Object 句柄可安全跨线程持有/传递。
    unsafe impl Send for JobObject {}
    unsafe impl Sync for JobObject {}

    impl JobObject {
        pub fn new() -> io::Result<Self> {
            // (null, null) = 匿名作业、默认安全属性
            let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }

            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

            let ok = unsafe {
                SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const core::ffi::c_void,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
            };
            if ok == 0 {
                let err = io::Error::last_os_error();
                unsafe { CloseHandle(handle) };
                return Err(err);
            }

            Ok(Self(handle))
        }

        /// 把子进程加入作业。**必须在进程创建后尽快调用**——
        /// 若 worker 在被加入之前就 fork 了后代，那些后代不在作业内。
        pub fn assign(&self, child: &Child) -> io::Result<()> {
            let proc_handle = child.as_raw_handle() as HANDLE;
            let ok = unsafe { AssignProcessToJobObject(self.0, proc_handle) };
            if ok == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        }
    }

    impl Drop for JobObject {
        fn drop(&mut self) {
            unsafe { CloseHandle(self.0) };
        }
    }
}

#[cfg(not(windows))]
mod platform {
    use std::io;
    use std::process::Child;

    /// 非 Windows 平台占位。本项目只面向 Windows；
    /// 这里保留接口是为了让 `cargo check` 在别的平台也能过。
    pub struct JobObject;

    impl JobObject {
        pub fn new() -> io::Result<Self> {
            Ok(Self)
        }
        pub fn assign(&self, _child: &Child) -> io::Result<()> {
            Ok(())
        }
    }
}

pub use platform::JobObject;

/// 创建 Job Object 并立即绑定子进程。失败**不算致命**——
/// 只是少了一道防线，EOF 自杀仍然有效，所以这里降级为警告。
pub fn bind_child(child: &std::process::Child) -> Option<JobObject> {
    match JobObject::new().and_then(|job| {
        job.assign(child)?;
        Ok(job)
    }) {
        Ok(job) => Some(job),
        Err(e) => {
            eprintln!("[worker] 警告：Job Object 绑定失败（{e}），退化为仅靠 stdin EOF 回收子进程");
            None
        }
    }
}
