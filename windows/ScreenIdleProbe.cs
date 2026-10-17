// Read-only Windows display and session notifications. Never sends input or power requests.
using System;
using System.Collections.Concurrent;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

public sealed class ScreenIdleProbe : IDisposable
{
    public sealed class Notice
    {
        public string Time;
        public string Kind;
        public int Value;
    }

    public sealed class Reading
    {
        public bool InputAvailable;
        public double IdleSeconds;
        public uint LastInputTick;
        public uint PowerQueryStatus;
        public uint ExecutionState;
        public uint ForegroundPid;
        public string ForegroundProcess;
        public int SessionId;
        public int ConsoleDisplay;
        public int SessionDisplay;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LastInput { public uint Size; public uint Tick; }

    [DllImport("user32.dll")] private static extern bool GetLastInputInfo(ref LastInput info);
    [DllImport("kernel32.dll")] private static extern uint GetTickCount();
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("powrprof.dll")] private static extern uint CallNtPowerInformation(int level, IntPtr input, uint inputSize, out uint output, uint outputSize);
    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr RegisterPowerSettingNotification(IntPtr window, ref Guid setting, uint flags);
    [DllImport("user32.dll")] private static extern bool UnregisterPowerSettingNotification(IntPtr handle);
    [DllImport("wtsapi32.dll", SetLastError = true)] private static extern bool WTSRegisterSessionNotification(IntPtr window, uint flags);
    [DllImport("wtsapi32.dll")] private static extern bool WTSUnRegisterSessionNotification(IntPtr window);
    [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

    private readonly ConcurrentQueue<Notice> notices = new ConcurrentQueue<Notice>();
    private readonly ManualResetEvent ready = new ManualResetEvent(false);
    private readonly Thread thread;
    private IntPtr windowHandle;
    private Exception startupError;
    private volatile int consoleDisplay = -1;
    private volatile int sessionDisplay = -1;

    public ScreenIdleProbe()
    {
        thread = new Thread(Run);
        thread.IsBackground = true;
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        if (!ready.WaitOne(10000)) throw new TimeoutException("Display listener did not start.");
        if (startupError != null) throw new InvalidOperationException("Display listener failed.", startupError);
    }

    private void Run()
    {
        try
        {
            using (var window = new Listener(this))
            {
                windowHandle = window.Handle;
                ready.Set();
                Application.Run();
            }
        }
        catch (Exception error) { startupError = error; ready.Set(); }
    }

    public Reading Sample()
    {
        var input = new LastInput { Size = (uint)Marshal.SizeOf(typeof(LastInput)) };
        bool available = GetLastInputInfo(ref input);
        uint flags;
        uint status = CallNtPowerInformation(16, IntPtr.Zero, 0, out flags, 4);
        uint pid;
        GetWindowThreadProcessId(GetForegroundWindow(), out pid);
        string name = null;
        try { using (var process = Process.GetProcessById((int)pid)) name = process.ProcessName; }
        catch (ArgumentException) { }
        catch (Win32Exception) { }
        return new Reading {
            InputAvailable = available, IdleSeconds = available ? unchecked(GetTickCount() - input.Tick) / 1000.0 : -1,
            LastInputTick = input.Tick, PowerQueryStatus = status, ExecutionState = flags,
            ForegroundPid = pid, ForegroundProcess = name, SessionId = Process.GetCurrentProcess().SessionId,
            ConsoleDisplay = consoleDisplay, SessionDisplay = sessionDisplay
        };
    }

    public Notice[] Drain()
    {
        var result = new System.Collections.Generic.List<Notice>();
        Notice notice;
        while (notices.TryDequeue(out notice)) result.Add(notice);
        return result.ToArray();
    }

    public void Dispose()
    {
        if (windowHandle != IntPtr.Zero) PostMessage(windowHandle, 0x0010, IntPtr.Zero, IntPtr.Zero);
        thread.Join(5000);
        ready.Dispose();
    }

    private sealed class Listener : NativeWindow, IDisposable
    {
        private readonly ScreenIdleProbe owner;
        private readonly IntPtr consoleRegistration;
        private readonly IntPtr sessionRegistration;
        private readonly bool sessionRegistered;
        private static readonly Guid ConsoleGuid = new Guid("6fe69556-704a-47a0-8f24-c28d936fda47");
        private static readonly Guid SessionGuid = new Guid("2b84c20e-ad23-4ddf-93db-05ffbd7efca5");

        public Listener(ScreenIdleProbe owner)
        {
            this.owner = owner;
            CreateHandle(new CreateParams { Caption = "ScreenIdleProbe" });
            Guid console = ConsoleGuid, session = SessionGuid;
            consoleRegistration = RegisterPowerSettingNotification(Handle, ref console, 0);
            sessionRegistration = RegisterPowerSettingNotification(Handle, ref session, 0);
            sessionRegistered = WTSRegisterSessionNotification(Handle, 0);
            if (consoleRegistration == IntPtr.Zero || sessionRegistration == IntPtr.Zero || !sessionRegistered)
            {
                int error = Marshal.GetLastWin32Error();
                Dispose();
                throw new Win32Exception(error, "Unable to subscribe to display/session notifications.");
            }
        }

        protected override void WndProc(ref Message message)
        {
            string kind = null;
            int value = message.WParam.ToInt32();
            if (message.Msg == 0x0218 && value == 0x8013 && message.LParam != IntPtr.Zero)
            {
                Guid setting = (Guid)Marshal.PtrToStructure(message.LParam, typeof(Guid));
                if (Marshal.ReadInt32(message.LParam, 16) == 4)
                {
                    value = Marshal.ReadInt32(message.LParam, 20);
                    if (setting == ConsoleGuid) { owner.consoleDisplay = value; kind = "console_display"; }
                    if (setting == SessionGuid) { owner.sessionDisplay = value; kind = "session_display"; }
                }
            }
            else if (message.Msg == 0x02B1) kind = "session_change";
            else if (message.Msg == 0x0218) kind = "power_broadcast";
            else if (message.Msg == 0x0010) { Application.ExitThread(); return; }
            if (kind != null) owner.notices.Enqueue(new Notice { Time = DateTimeOffset.Now.ToString("o"), Kind = kind, Value = value });
            base.WndProc(ref message);
        }

        public void Dispose()
        {
            if (consoleRegistration != IntPtr.Zero) UnregisterPowerSettingNotification(consoleRegistration);
            if (sessionRegistration != IntPtr.Zero) UnregisterPowerSettingNotification(sessionRegistration);
            if (sessionRegistered) WTSUnRegisterSessionNotification(Handle);
            DestroyHandle();
        }
    }
}
