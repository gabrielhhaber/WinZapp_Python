"""Build a hidden guardian around the existing batch installer.

The child starts suspended inside a kill-on-close Job Object. If the
guardian dies, its batch AND xcopy children die with it; a lock released by
a dead guardian therefore cannot leave an orphan copy writing the install.
Relaunch is always performed by the application-created, outside waiter.
"""
import os
from core.update_guard_diagnostics import powershell_reporter

_JOB_SOURCE = r'''
using System;
using System.Text;
using System.IO;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class WinZappInstallJob {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  struct STARTUPINFO {
    public int cb; public string reserved, desktop, title;
    public int x,y,xsize,ysize,xchars,ychars,fill,flags;
    public short show, reservedSize;
    public IntPtr reservedPtr, input, output, error;
  }
  [StructLayout(LayoutKind.Sequential)]
  struct PROCESSINFO { public IntPtr process, thread; public uint pid, tid; }
  [StructLayout(LayoutKind.Sequential)]
  struct BASICLIMIT {
    public long processTime, jobTime; public uint flags;
    public UIntPtr minWork, maxWork; public uint active;
    public UIntPtr affinity; public uint priority, scheduling;
  }
  [StructLayout(LayoutKind.Sequential)]
  struct IOLIMIT { public ulong a,b,c,d,e,f; }
  [StructLayout(LayoutKind.Sequential)]
  struct LIMIT {
    public BASICLIMIT basic; public IOLIMIT io;
    public UIntPtr processMem, jobMem, peakProcess, peakJob;
  }
  [StructLayout(LayoutKind.Sequential)]
  struct ACCOUNTING {
    public long a,b,c,d; public uint faults,total,active,terminated;
  }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern IntPtr CreateJobObjectW(IntPtr attr, string name);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool SetInformationJobObject(IntPtr job, int kind, ref LIMIT info, uint size);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern bool CreateProcessW(string app, StringBuilder command, IntPtr pa,
    IntPtr ta, bool inherit, uint flags, IntPtr env, string cwd,
    ref STARTUPINFO startup, out PROCESSINFO process);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern uint ResumeThread(IntPtr thread);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool GetExitCodeProcess(IntPtr process, out uint code);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool TerminateProcess(IntPtr process, uint code);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool TerminateJobObject(IntPtr job, uint code);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool QueryInformationJobObject(IntPtr job, int kind,
    out ACCOUNTING accounting, uint size, IntPtr returned);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  static void Report(string path, string value) {
    try { File.WriteAllText(path, value, Encoding.ASCII); } catch { }
  }
  static void Check(bool ok) { if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error()); }
  public static uint Run(string batch, string ready, string diagnostic) {
    IntPtr job = IntPtr.Zero;
    PROCESSINFO pi = new PROCESSINFO();
    bool assigned = false;
    string stage = "job_create";
    try {
      Report(diagnostic, "stage:" + stage);
      job = CreateJobObjectW(IntPtr.Zero, null); Check(job != IntPtr.Zero);
      stage = "job_configure"; Report(diagnostic, "stage:" + stage);
      LIMIT limit = new LIMIT(); limit.basic.flags = 0x2000;
      Check(SetInformationJobObject(job, 9, ref limit, (uint)Marshal.SizeOf(limit)));
      string cmd = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "cmd.exe");
      STARTUPINFO si = new STARTUPINFO(); si.cb = Marshal.SizeOf(si);
      stage = "process_create"; Report(diagnostic, "stage:" + stage);
      Check(CreateProcessW(cmd, new StringBuilder("\"" + cmd + "\" /d /s /c \"\"" + batch + "\"\""),
        IntPtr.Zero, IntPtr.Zero, false, 0x08000004, IntPtr.Zero, null, ref si, out pi));
      stage = "job_assign"; Report(diagnostic, "stage:" + stage);
      Check(AssignProcessToJobObject(job, pi.process)); assigned = true;
      stage = "process_resume"; Report(diagnostic, "stage:" + stage);
      Check(ResumeThread(pi.thread) != 0xffffffff);
      stage = "ready"; Report(diagnostic, "stage:" + stage);
      File.WriteAllText(ready + ".tmp", "ready:" + System.Diagnostics.Process.GetCurrentProcess().Id + "\n");
      File.Move(ready + ".tmp", ready);
      stage = "running"; Report(diagnostic, "stage:" + stage);
      Check(WaitForSingleObject(pi.process, 0xffffffff) == 0);
      stage = "exit_code";
      uint result; Check(GetExitCodeProcess(pi.process, out result));
      Report(diagnostic, "completed:" + result); return result;
    } catch (Exception error) {
      Win32Exception native = error as Win32Exception;
      Report(diagnostic, "error:" + stage + ":" + (native == null ? 0 : native.NativeErrorCode));
      throw;
    } finally {
      if (pi.process != IntPtr.Zero && !assigned) {
        TerminateProcess(pi.process, 1); WaitForSingleObject(pi.process, 0xffffffff);
      }
      if (job != IntPtr.Zero) {
        TerminateJobObject(job, 1);
        ACCOUNTING accounting;
        while (QueryInformationJobObject(job, 1, out accounting,
                 (uint)Marshal.SizeOf(typeof(ACCOUNTING)), IntPtr.Zero) && accounting.active > 0)
          System.Threading.Thread.Sleep(25);
        CloseHandle(job);
      }
      if (pi.thread != IntPtr.Zero) CloseHandle(pi.thread);
      if (pi.process != IntPtr.Zero) CloseHandle(pi.process);
    }
  }
}
'''


def _ps(value):
    if any(c in value for c in ('\x00', '\r', '\n')):
        raise ValueError("invalid guardian path")
    return "'" + value.replace("'", "''") + "'"


def guardian_script(batch_path, paths, package_lease=""):
    """Only generated text; tests never execute this Windows process code."""
    package = (
        "$phase = 'package_lock'\nWrite-Diagnostic 'stage:package_lock'\n"
        f"$package = [IO.File]::Open({_ps(package_lease)}, 'OpenOrCreate', 'Read', 'Read')\n"
        if package_lease else ""
    )
    return (
        "$ErrorActionPreference = 'Stop'\n$guard = $null\n$package = $null\n"
        + powershell_reporter(_ps(paths['diagnostic']))
        + "$phase = 'compile'\nWrite-Diagnostic 'stage:compile'\n"
        "try {\n"
        "Add-Type -TypeDefinition @'\n" + _JOB_SOURCE + "\n'@\n"
        "  $phase = 'guard_lock'\nWrite-Diagnostic 'stage:guard_lock'\n"
        f"  $guard = [IO.File]::Open({_ps(paths['lock'])}, 'OpenOrCreate', 'ReadWrite', 'Read')\n"
        + package +
        "  $phase = 'native'\n"
        f"  $code = [WinZappInstallJob]::Run({_ps(batch_path)}, {_ps(paths['ready'])}, {_ps(paths['diagnostic'])})\n"
        "  exit ([int]$code)\n"
        "} catch {\n"
        "  if ($phase -ne 'native') { Write-Diagnostic ('error:' + $phase + ':0') }\n"
        "  exit 1\n} finally {\n"
        "  if ($package) { $package.Dispose() }\n"
        "  if ($guard) { $guard.Dispose() }\n}\n"
    )


def launcher_script(guardian_path, powershell_path):
    """All batch-carried paths must already be console-safe."""
    for path in (guardian_path, powershell_path):
        if any(c in path for c in ('%', '!', '\r', '\n', '"')):
            raise ValueError("path cannot be represented safely in batch")
    return (
        "@echo off\nsetlocal DisableDelayedExpansion\n"
        f'"{powershell_path}" -NoProfile -NonInteractive -ExecutionPolicy Bypass '
        f'-File "{guardian_path}"\nexit /b %ERRORLEVEL%\n'
    )


def powershell_path():
    """Use the Windows system host rather than searching the user PATH."""
    return os.path.join(os.environ["SystemRoot"], "System32", "WindowsPowerShell",
                        "v1.0", "powershell.exe")
