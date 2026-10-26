<#
.SYNOPSIS
    Authenticode-sign this project's own binaries with the Bonux Apps
    certificate, and verify what came out.

.DESCRIPTION
    The private key is not on this machine. It lives in Certum's cloud and is
    reached through SimplySign Desktop, which publishes the certificate into
    CurrentUser\My while a session is connected — so there is no PFX to find,
    no CSR to make, and nothing here that could leak a key. If the session has
    lapsed the certificate simply is not there, and this script says so rather
    than producing an unsigned build that looks like a signed one.

    Only our own executables are signed. Everything under `_internal` is the
    Python runtime and the CRT, already signed by the PSF and by Microsoft;
    re-signing those would replace a publisher's attestation with ours for
    code we did not write.

.PARAMETER Path
    Files to sign. Defaults to the four executables of a built one-folder
    tree plus any installer sitting in dist\.

.PARAMETER VerifyOnly
    Check what is already signed and change nothing.

.PARAMETER InnoCommand
    Print the command Inno Setup needs for its own SignTool, and exit. Inno
    signs the setup binary and the uninstaller itself, and this keeps the
    tool path, the thumbprint and the timestamp URL defined in exactly one
    place rather than spelled out again in build.py and installer.iss.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\Sign-Windows.ps1
    powershell -ExecutionPolicy Bypass -File scripts\Sign-Windows.ps1 -VerifyOnly

.NOTES
    A PIN may be asked for in a Windows dialog the first time a session signs
    anything. That prompt belongs to SimplySign: answer it there, never
    anywhere else.
#>
# PositionalBinding off: with the file list taking the remaining arguments,
# anything positional would swallow the first paths into -Thumbprint and
# -TimestampUrl instead, and the failure reads as a missing certificate.
[CmdletBinding(PositionalBinding = $false)]
param(
    # Remaining arguments, so `Sign-Windows.ps1 a.exe b.exe` works: powershell
    # -File hands a script its arguments one at a time, and a plain [string[]]
    # positional takes only the first of them.
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Path,
    # Bonux Apps / Oleksii Holiuk, issued by Certum Code Signing 2021 CA.
    [string]   $Thumbprint = 'D875328A72DF60A5BD74CB38E418C54B16A60BE9',
    [string]   $TimestampUrl = 'http://time.certum.pl',
    [switch]   $VerifyOnly,
    [switch]   $InnoCommand
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Resolve-SignTool {
    <# The newest x64 SignTool in the installed Windows SDKs. #>
    $roots = @("${env:ProgramFiles(x86)}\Windows Kits\10\bin",
               "$env:ProgramFiles\Windows Kits\10\bin")
    $found = foreach ($r in $roots) {
        if (Test-Path $r) {
            Get-ChildItem $r -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match '\\x64\\' }
        }
    }
    $best = $found | Sort-Object { $_.Directory.Parent.Name } -Descending | Select-Object -First 1
    if (-not $best) {
        $cmd = Get-Command signtool -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        throw "SignTool not found. Install the Windows SDK's Signing Tools."
    }
    return $best.FullName
}

function Get-SigningCertificate {
    <# The certificate, checked before anything is handed to SignTool: an
       expired or wrong-purpose certificate fails per file otherwise, halfway
       through a build, with an error code instead of a reason. #>
    $cert = Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
            Where-Object { $_.Thumbprint -eq $Thumbprint }
    if (-not $cert) {
        throw ("Certificate $Thumbprint is not in CurrentUser\My. " +
               "Open SimplySign Desktop and use Connect to SimplySign, " +
               "then run this again.")
    }
    if (-not $cert.HasPrivateKey) {
        throw "Certificate $Thumbprint has no usable private key - the SimplySign session is not connected."
    }
    $now = Get-Date
    if ($now -lt $cert.NotBefore -or $now -gt $cert.NotAfter) {
        throw "Certificate is outside its validity window ($($cert.NotBefore) .. $($cert.NotAfter))."
    }
    $codeSigning = $cert.EnhancedKeyUsageList | Where-Object { $_.ObjectId -eq '1.3.6.1.5.5.7.3.3' }
    if (-not $codeSigning) {
        throw "Certificate is not valid for Code Signing (missing EKU 1.3.6.1.5.5.7.3.3)."
    }
    $chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain
    $chain.ChainPolicy.RevocationMode = 'Online'
    $chain.ChainPolicy.RevocationFlag = 'ExcludeRoot'
    if (-not $chain.Build($cert)) {
        $why = ($chain.ChainStatus | ForEach-Object { $_.StatusInformation.Trim() }) -join '; '
        throw "Certificate chain does not validate: $why"
    }
    return $cert
}

function Get-DefaultTargets {
    <# Ours, and only ours. #>
    $tree = Join-Path $root 'dist\ClaudeTelegram'
    $names = 'ClaudeTelegram.exe', 'claudetg-daemon.exe', 'hookc.exe', 'statusline.exe'
    $targets = foreach ($n in $names) {
        $p = Join-Path $tree $n
        if (Test-Path $p) { $p }
    }
    # The newest installer only. dist\ keeps every setup ever built, and all
    # the older ones are from before any of this existed — verifying the lot
    # reports eighteen missing signatures and buries the one that matters.
    $setup = Get-ChildItem (Join-Path $root 'dist') -Filter 'ClaudeTelegram-Setup-*.exe' -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $targets + ($setup | ForEach-Object { $_.FullName })
}

function Show-Signature {
    param([string] $File)
    $sig = Get-AuthenticodeSignature $File
    $thumb = if ($sig.SignerCertificate) { $sig.SignerCertificate.Thumbprint } else { '-' }
    $stamp = if ($sig.TimeStamperCertificate) { 'timestamped' } else { 'NO TIMESTAMP' }
    $mine = if ($thumb -eq $Thumbprint) { 'ours' } else { 'other' }
    $hash = (Get-FileHash $File -Algorithm SHA256).Hash.ToLower()
    [pscustomobject]@{
        File      = Split-Path -Leaf $File
        Status    = [string]$sig.Status
        Signer    = $mine
        Timestamp = $stamp
        SHA256    = $hash
    }
}

$signtool = Resolve-SignTool

if ($InnoCommand) {
    # $q is Inno's own way of writing a quote, which keeps this free of the
    # nested quoting that a path with spaces in it would otherwise need; $f
    # is the file Inno wants signed.
    Write-Output ("`$qSIGNTOOL`$q sign /fd sha256 /tr TSURL /td sha256 /sha1 THUMB `$f" `
        -replace 'SIGNTOOL', $signtool -replace 'TSURL', $TimestampUrl -replace 'THUMB', $Thumbprint)
    exit 0
}

if (-not $Path) { $Path = Get-DefaultTargets }
$Path = @($Path | Where-Object { $_ } | ForEach-Object { (Resolve-Path $_).Path })
if (-not $Path) { throw "Nothing to sign: build first (python build.py --no-sign) or pass -Path." }

Write-Host "SignTool : $signtool"
Write-Host "Files    : $($Path.Count)"

if (-not $VerifyOnly) {
    $cert = Get-SigningCertificate
    Write-Host "Certificate: $($cert.Subject)"
    Write-Host "Valid until: $($cert.NotAfter)  ($([int]($cert.NotAfter - (Get-Date)).TotalDays) days)"
    Write-Host ""
    # One SignTool call for the whole set: SimplySign asks for the PIN once
    # per session, and a call per file turns that into a prompt per file.
    $args = @('sign', '/fd', 'sha256', '/tr', $TimestampUrl, '/td', 'sha256',
              '/sha1', $Thumbprint, '/v') + $Path
    & $signtool @args
    if ($LASTEXITCODE -ne 0) { throw "SignTool failed with exit code $LASTEXITCODE" }
    Write-Host ""
}

Write-Host "=== verify (signtool /pa /all /tw) ==="
& $signtool verify /pa /all /tw /v @Path | Out-Null
$verified = $LASTEXITCODE -eq 0
if (-not $verified) { Write-Warning "signtool verify reported exit code $LASTEXITCODE" }

$report = $Path | ForEach-Object { Show-Signature $_ }
$report | Format-Table -AutoSize
if (-not $verified) { throw "Verification failed." }
if ($report | Where-Object { $_.Status -ne 'Valid' -or $_.Signer -ne 'ours' -or $_.Timestamp -ne 'timestamped' }) {
    throw "At least one file is not validly signed by us with a timestamp."
}
Write-Host "All $($report.Count) file(s) valid, signed by $Thumbprint, timestamped."
