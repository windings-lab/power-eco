# Power eco mode for Windows (CPU via RAPL MSR + powercfg, NVIDIA GPU via nvidia-smi.exe).
#   power-eco.ps1 start [W] save current settings, apply eco limits (optional CPU watts)
#   power-eco.ps1 stop     restore the saved settings
#   power-eco.ps1 status   show current values (no admin needed)
#   power-eco.ps1 rapl     show the CPU package power limits (admin)
# Needs admin for start/stop/rapl; it re-launches itself elevated (UAC prompt).
# Under WSL it is called by the `power-eco` bash script.
param(
    [Parameter(Position = 0)][ValidateSet('start', 'stop', 'status', 'rapl')][string]$Action = 'status',
    [Parameter(Position = 1)][ValidateRange(0, 250)][int]$Watts = 0
)

# ---- eco limits (same intent as the Linux script)
$CpuPlW    = 25      # RAPL PL1 = PL2, W (Linux: constraint_0/1_power_limit_uw). 0 = don't touch.
if ($Watts -gt 0) { $CpuPlW = $Watts }
$CpuMaxPct = 60      # PROCTHROTTLEMAX: max processor state, % (Linux: max_perf_pct)
$CpuEpp    = 100     # PERFEPP: 0 = performance ... 100 = power saving
$CpuBoost  = 0       # PERFBOOSTMODE: 0 = disabled (Linux: no_turbo=1)
$GpuClk    = '210,1000'
$GpuMemClk = '405,810'
$GpuPlW    = 100
# Note: on this PC (Hyper-V/WSL2 on) Windows ignores the powercfg CPU caps;
# the RAPL power limit is the one that actually works.

$Dir   = Join-Path $env:LOCALAPPDATA 'power-eco'
$State = Join-Path $Dir 'state.json'
$Log   = Join-Path $Dir 'last.log'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

# ---- RAPL via PawnIO (signed kernel driver, installed with LibreHardwareMonitor).
# The IntelMSR module >= 0.2.11 allows writing MSR_PKG_POWER_LIMIT (0x610).
$PawnIOLib     = 'C:\Program Files\PawnIO\PawnIOLib.dll'
$MsrModule     = Join-Path $Dir 'IntelMSR.bin'
$MsrModuleUrl  = 'https://github.com/namazso/PawnIO.Modules/releases/download/0.2.11/release_0_2_11.zip'
$MsrModuleHash = 'D6ED85D65AB17A22F813EF98207D6D537155EE2DED5976A21CB48413C9B92E5F'  # SHA256 of IntelMSR.bin

function Get-MsrModule {
    if ((Test-Path $MsrModule) -and (Get-FileHash $MsrModule -Algorithm SHA256).Hash -eq $MsrModuleHash) { return }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $tmp = Join-Path $env:TEMP ('pawnio-' + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        Invoke-WebRequest $MsrModuleUrl -OutFile "$tmp\m.zip" -UseBasicParsing
        Expand-Archive "$tmp\m.zip" -DestinationPath $tmp
        if ((Get-FileHash "$tmp\IntelMSR.bin" -Algorithm SHA256).Hash -ne $MsrModuleHash) { throw 'IntelMSR.bin hash mismatch' }
        Copy-Item "$tmp\IntelMSR.bin" $MsrModule -Force
    } finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

function Open-Msr {
    if (-not (Test-Path $PawnIOLib)) { throw "PawnIO not installed ($PawnIOLib); install LibreHardwareMonitor" }
    Get-MsrModule
    if (-not ('Rapl' -as [type])) {
        Add-Type -TypeDefinition (@'
using System; using System.Runtime.InteropServices;
public static class Rapl {
  const string L = @"__LIB__";
  [DllImport(L)] static extern int pawnio_open(out IntPtr h);
  [DllImport(L)] static extern int pawnio_load(IntPtr h, byte[] blob, UIntPtr size);
  [DllImport(L)] static extern int pawnio_execute(IntPtr h, string name, ulong[] inp, UIntPtr inSize, ulong[] outp, UIntPtr outSize, out UIntPtr ret);
  [DllImport(L)] static extern int pawnio_close(IntPtr h);
  static IntPtr h = IntPtr.Zero;
  static void Check(int hr, string what) { if (hr != 0) throw new Exception(String.Format("{0} failed: 0x{1:X8}", what, hr)); }
  public static void Open(byte[] blob) {
    if (h != IntPtr.Zero) return;
    Check(pawnio_open(out h), "pawnio_open");
    Check(pawnio_load(h, blob, (UIntPtr)blob.Length), "pawnio_load");
  }
  public static void Close() { if (h != IntPtr.Zero) { pawnio_close(h); h = IntPtr.Zero; } }
  public static ulong Read(uint msr) {
    ulong[] o = new ulong[1]; UIntPtr r;
    Check(pawnio_execute(h, "ioctl_read_msr", new ulong[] { msr }, (UIntPtr)1, o, (UIntPtr)1, out r), String.Format("read MSR 0x{0:X}", msr));
    return o[0];
  }
  public static void Write(uint msr, ulong v) {
    UIntPtr r;
    Check(pawnio_execute(h, "ioctl_write_msr", new ulong[] { msr, v }, (UIntPtr)2, new ulong[0], (UIntPtr)0, out r), String.Format("write MSR 0x{0:X}", msr));
  }
  // MSR_RAPL_POWER_UNIT bits 3:0 -> power unit = 1/2^n W
  public static double Unit() { return Math.Pow(2, -(int)(Read(0x606) & 0xF)); }
  public static bool Locked(ulong v) { return (v >> 63) != 0; }
  public static string Describe(ulong v) {
    double u = Unit();
    return String.Format(System.Globalization.CultureInfo.InvariantCulture,
      "PL1={0:0.#} W ({1}), PL2={2:0.#} W ({3}){4}  [0x{5:X16}]",
      (v & 0x7FFF) * u, ((v >> 15) & 1) == 1 ? "on" : "off",
      ((v >> 32) & 0x7FFF) * u, ((v >> 47) & 1) == 1 ? "on" : "off",
      Locked(v) ? ", LOCKED by BIOS" : "", v);
  }
  // Set PL1 and PL2 to `watts`, enable both, keep time windows/clamp bits as they are.
  public static ulong WithLimit(ulong raw, double watts) {
    ulong n = (ulong)Math.Round(watts / Unit()) & 0x7FFF;
    raw &= ~(0x7FFFUL | (0x7FFFUL << 32));
    return raw | n | (1UL << 15) | (n << 32) | (1UL << 47);
  }
}
'@).Replace('__LIB__', $PawnIOLib)
    }
    [Rapl]::Open([IO.File]::ReadAllBytes($MsrModule))
}

function Show-Rapl {
    try { Open-Msr; 'RAPL ' + [Rapl]::Describe([Rapl]::Read(0x610)) }
    catch { "RAPL: $($_.Exception.Message)" }
}

$Settings = @(
    @{ Key = 'max';   Sub = 'SUB_PROCESSOR'; Name = 'PROCTHROTTLEMAX'; Eco = $CpuMaxPct },
    @{ Key = 'max1';  Sub = 'SUB_PROCESSOR'; Name = 'PROCTHROTTLEMAX1'; Eco = $CpuMaxPct },
    @{ Key = 'epp';   Sub = 'SUB_PROCESSOR'; Name = 'PERFEPP';         Eco = $CpuEpp },
    @{ Key = 'boost'; Sub = 'SUB_PROCESSOR'; Name = 'PERFBOOSTMODE';   Eco = $CpuBoost }
)

function Get-Ac($sub, $name) {
    # Locale-independent: the 2nd-to-last 0x........ in the output is "Current AC", the last is "Current DC".
    $m = @(powercfg /qh SCHEME_CURRENT $sub $name | Select-String -AllMatches '0x[0-9a-fA-F]{8}' |
        ForEach-Object { $_.Matches } | ForEach-Object { $_.Value })
    if ($m.Count -ge 2) { [Convert]::ToInt32($m[$m.Count - 2], 16) } else { $null }
}

function Invoke-NvSmi { & nvidia-smi @args 2>&1 | ForEach-Object { "  nvidia-smi $($args -join ' '): $_" } }

function Show-Status {
    foreach ($s in $Settings) { "{0,-16} AC = {1}" -f $s.Name, (Get-Ac $s.Sub $s.Name) }
    $c = (Get-Counter '\Processor Information(_Total)\Processor Frequency', '\Processor Information(_Total)\% of Maximum Frequency' -ErrorAction SilentlyContinue).CounterSamples
    if ($c) { "cpu now: {0:N0} MHz ({1:N0}% of max)" -f $c[0].CookedValue, $c[1].CookedValue }
    "active scheme: " + ((powercfg /getactivescheme) -replace '^.*?:\s*', '')
    nvidia-smi --query-gpu=power.limit,clocks.max.gr,clocks.max.mem --format=csv 2>&1
    "state file: " + $(if (Test-Path $State) { 'present (eco is on)' } else { 'absent' })
}

function Start-Eco {
    if (-not (Test-Path $State)) {
        $saved = @{}
        foreach ($s in $Settings) { $saved[$s.Key] = Get-Ac $s.Sub $s.Name }
        $saved.gpu_pl = (nvidia-smi --query-gpu=power.limit '--format=csv,noheader,nounits').Trim()
        $saved | ConvertTo-Json | Set-Content $State
    }
    if ($CpuPlW -gt 0) {
        try {
            Open-Msr
            $raw = [Rapl]::Read(0x610)
            'RAPL before: ' + [Rapl]::Describe($raw)
            $st = Get-Content $State -Raw | ConvertFrom-Json
            if (-not $st.rapl) {  # keep the original stock value across repeated starts
                $st | Add-Member -NotePropertyName rapl -NotePropertyValue ('0x{0:X16}' -f $raw) -Force
                $st | ConvertTo-Json | Set-Content $State
            }
            if ([Rapl]::Locked($raw)) { 'warning: RAPL power limit is locked by BIOS, cannot change it' }
            else {
                [Rapl]::Write(0x610, [Rapl]::WithLimit($raw, $CpuPlW))
                'RAPL after:  ' + [Rapl]::Describe([Rapl]::Read(0x610))
            }
        } catch { "warning: CPU power limit not applied: $($_.Exception.Message)" }
    }
    foreach ($s in $Settings) {
        powercfg /setacvalueindex SCHEME_CURRENT $s.Sub $s.Name $s.Eco
        if ($LASTEXITCODE -ne 0) { "warning: could not set $($s.Name)" }
    }
    powercfg /setactive SCHEME_CURRENT
    Invoke-NvSmi -lgc $GpuClk
    Invoke-NvSmi -lmc $GpuMemClk
    Invoke-NvSmi -pl $GpuPlW
    'eco ON'
}

function Stop-Eco {
    if (-not (Test-Path $State)) { 'no saved state, nothing to restore'; return }
    $saved = Get-Content $State -Raw | ConvertFrom-Json
    foreach ($s in $Settings) {
        $v = $saved.($s.Key)
        if ($null -eq $v -and $s.Key -eq 'max1') { $v = 100 }  # state saved before this setting existed
        if ($null -ne $v) { powercfg /setacvalueindex SCHEME_CURRENT $s.Sub $s.Name $v }
    }
    powercfg /setactive SCHEME_CURRENT
    if ($saved.rapl) {
        try {
            Open-Msr
            [Rapl]::Write(0x610, [Convert]::ToUInt64($saved.rapl.Substring(2), 16))
            'RAPL restored: ' + [Rapl]::Describe([Rapl]::Read(0x610))
        } catch { "warning: CPU power limit not restored: $($_.Exception.Message) (a reboot resets it)" }
    }
    Invoke-NvSmi -rgc
    Invoke-NvSmi -rmc
    $pl = "$($saved.gpu_pl)".Trim()
    if ($pl -notmatch '^\d+(\.\d+)?$') {  # bad/missing saved value: fall back to the card's default limit
        $pl = (nvidia-smi --query-gpu=power.default_limit '--format=csv,noheader,nounits').Trim()
    }
    Invoke-NvSmi -pl $pl
    Remove-Item $State -Force
    'eco OFF (settings restored)'
}

if ($Action -eq 'status') { Show-Status; return }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    if ($env:POWER_ECO_ELEVATED) { throw "elevated but not admin, refusing to relaunch" }
    $env:POWER_ECO_ELEVATED = "1"
    Remove-Item $Log -Force -ErrorAction SilentlyContinue
    $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", $Action, $Watts)
    if (Test-Path $Log) { Get-Content $Log }
    exit $p.ExitCode
}

& {
    switch ($Action) { 'start' { Start-Eco } 'stop' { Stop-Eco } 'rapl' { Show-Rapl } }
    if ('Rapl' -as [type]) { [Rapl]::Close() }
} *>&1 | Tee-Object -FilePath $Log
