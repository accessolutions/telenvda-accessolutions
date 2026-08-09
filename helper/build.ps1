# Builds the screen sharing helper and places it where the add-on looks for it.
#
# A thirty two bit build is produced by default because it runs on every version
# of Windows the add-on supports, whatever the version of NVDA it runs under.

[CmdletBinding()]
param(
	[ValidateSet('386', 'amd64')]
	[string]$Architecture = '386'
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$output = Join-Path $here '..\addon\globalPlugins\remoteClient\helpers\telenvda_screenshare.exe'
$outputDir = Split-Path -Parent $output
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

Push-Location $here
try {
	$env:GOOS = 'windows'
	$env:GOARCH = $Architecture
	# The symbol table is of no use here and accounts for a large part of the size
	# of the program, which is shipped inside the add-on.
	go build -trimpath -ldflags '-s -w' -o $output .
	if ($LASTEXITCODE -ne 0) {
		throw "The helper could not be built."
	}
	Write-Output ("Built " + (Resolve-Path $output))
}
finally {
	Pop-Location
	Remove-Item Env:GOOS -ErrorAction SilentlyContinue
	Remove-Item Env:GOARCH -ErrorAction SilentlyContinue
}
