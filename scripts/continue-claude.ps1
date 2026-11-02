param(
    [int]$WaitHours = 2,
    [int]$WaitMinutes = 0,
    [string]$LogPath = (Join-Path $env:TEMP "claude-continue.log"),
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArguments
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    throw "Claude Code was not found on PATH. Install it or open a terminal where 'claude' is available."
}

$delay = [TimeSpan]::FromHours($WaitHours) + [TimeSpan]::FromMinutes($WaitMinutes)
$rateLimitPattern = '(?i)(rate.?limit|usage.?limit|limit.*reset|reset.*limit|try again.*(hour|minute)|resets? in)'
$firstRun = $true

while ($true) {
    if ($firstRun) {
        $commandArguments = $ClaudeArguments
        Write-Host "Starting Claude Code..."
    }
    else {
        $commandArguments = @("--continue")
        Write-Host "Resuming the most recent Claude Code conversation..."
    }

    $output = @(& claude @commandArguments 2>&1 | Tee-Object -FilePath $LogPath)
    $exitCode = $LASTEXITCODE
    $outputText = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine

    if ($exitCode -eq 0) {
        exit 0
    }

    if ($outputText -notmatch $rateLimitPattern) {
        Write-Host "Claude Code exited with code $exitCode without a detected rate limit. See $LogPath"
        exit $exitCode
    }

    $resumeAt = (Get-Date).Add($delay)
    Write-Host "Rate limit detected. Waiting until $($resumeAt.ToString('yyyy-MM-dd HH:mm:ss')) before retrying."
    Start-Sleep -Seconds ([int]$delay.TotalSeconds)
    $firstRun = $false
}