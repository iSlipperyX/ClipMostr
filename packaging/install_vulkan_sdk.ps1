# Installs the Vulkan SDK on a GitHub Actions Windows runner, for building whisper.cpp for the
# graphics card (packaging/build_whisper_cpp.py --vulkan). Only the build needs it; ClipMonster on a
# PC uses the graphics driver's own Vulkan.
param([string]$Version = "1.4.313.2")
$ErrorActionPreference = "Stop"
$installer = Join-Path $env:RUNNER_TEMP "vulkan-sdk.exe"
$urls = @(
  "https://sdk.lunarg.com/sdk/download/$Version/windows/vulkansdk-windows-X64-$Version.exe",
  "https://sdk.lunarg.com/sdk/download/$Version/windows/VulkanSDK-$Version-Installer.exe"
)
$got = $false
foreach ($url in $urls) {
  curl.exe -fsSL -o $installer $url
  if ($LASTEXITCODE -eq 0) { $got = $true; break }
}
if (-not $got) { throw "Couldn't download the Vulkan SDK $Version" }
$p = Start-Process -FilePath $installer -ArgumentList "--accept-licenses", "--default-answer", "--confirm-command", "install" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "The Vulkan SDK installer failed ($($p.ExitCode))" }
$sdk = "C:\VulkanSDK\$Version"
if (-not (Test-Path "$sdk\Bin\glslc.exe")) { throw "No glslc in $sdk" }
Add-Content $env:GITHUB_ENV "VULKAN_SDK=$sdk"
Add-Content $env:GITHUB_PATH "$sdk\Bin"
Write-Output "Vulkan SDK $Version in $sdk"
