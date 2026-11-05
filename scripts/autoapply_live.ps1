<#
Runs the auto-apply worker on THIS PC against your hosted (Vercel + Neon) JobAgent data.

Why a PC: the worker drives a real browser (Playwright/Chromium), which Vercel cannot run. In visible
(headed) mode it opens each policy-approved application, fills it with your confirmed facts and the
job-tailored resume, and - where the site shows a CAPTCHA - leaves the final "Submit" click to you.

  powershell -ExecutionPolicy Bypass -File scripts\autoapply_live.ps1            # fill-for-you, visible browser
  powershell -ExecutionPolicy Bypass -File scripts\autoapply_live.ps1 -DryRun    # fill + screenshot, never submits
  powershell -ExecutionPolicy Bypass -File scripts\autoapply_live.ps1 -Limit 1

The production environment variables are downloaded to .env.vercel (git-ignored, contains secrets;
delete it when you no longer need it).
#>
param([switch]$DryRun, [int]$Limit = 0)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Virtualenv not found. Run the README installation steps first." }

& $Python -c "import playwright" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install -r requirements-worker.txt
    & $Python -m playwright install chromium
}

$env:NODE_OPTIONS = "--dns-result-order=ipv4first"
npx --yes vercel@latest env pull .env.vercel --environment=production --yes | Out-Null
if (-not (Test-Path ".env.vercel")) { throw "Could not pull Vercel environment (run 'npx vercel login' first)." }

$env:JOBAGENT_ENV_FILE = Join-Path $Root ".env.vercel"
$argsList = @("-m", "jobagent", "autoapply", "--headed")
if ($DryRun) { $argsList += "--dry-run" }
if ($Limit -gt 0) { $argsList += @("--limit", "$Limit") }
& $Python @argsList
