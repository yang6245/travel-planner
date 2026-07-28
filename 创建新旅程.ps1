<#
.SYNOPSIS
  一键创建新旅程脚本（独立网站生成器）
.DESCRIPTION
  1. 创建 3 个新的 JSONBlob 云端容器（主数据/详情/图片）
  2. 复制源文件并替换云端配置与旅程名称
  3. 生成独立的新旅程 HTML 文件（每个旅程 = 一个独立网站）
#>

param(
    [string]$JourneyName = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# 设置控制台输出编码为 UTF-8，确保中文显示正常
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    chcp 65001 > $null 2>&1
} catch {}

# ---------- 工具函数 ----------
function Write-Step($msg) { Write-Host "[步骤] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[完成] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[提示] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[错误] $msg" -ForegroundColor Red }

# ---------- 1. 输入旅程名称 ----------
if (-not $JourneyName) {
    Write-Host ""
    Write-Host "==================================" -ForegroundColor Cyan
    Write-Host "   一键创建新旅程（独立网站）" -ForegroundColor Cyan
    Write-Host "==================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "请输入新旅程名称（例如：川西环线5日游）" -ForegroundColor White
    Write-Host "输入后按回车键确认：" -ForegroundColor White
    # 用 [Console]::ReadLine 替代 Read-Host，兼容性更好
    $JourneyName = [Console]::In.ReadLine()
    if ($JourneyName) { $JourneyName = $JourneyName.Trim() }
}

if (-not $JourneyName -or $JourneyName -eq "") {
    Write-Err "旅程名称不能为空，脚本已退出。"
    Start-Sleep -Seconds 3
    exit 1
}

# ---------- 2. 校验源文件 ----------
$SourceFile = "格聂南线6日行程.html"
$sourcePath = Join-Path $ProjectRoot $SourceFile
if (-not (Test-Path $sourcePath)) {
    Write-Err "源文件不存在：$sourcePath"
    Start-Sleep -Seconds 3
    exit 1
}

Write-Step "源文件：$SourceFile"

# ---------- 3. 创建 JSONBlob 云端容器 ----------
Write-Step "正在创建云端存储容器（共 3 个）..."

$initMain = @{
    _updatedAt  = [int64](Get-Date -UFormat %s)
    tripName    = $JourneyName
    subtitle    = ""
    totalBudget = 0
    days        = @()
    expenses    = @()
} | ConvertTo-Json -Depth 10

$initDetail = @{
    _updatedAt = [int64](Get-Date -UFormat %s)
    days       = @()
} | ConvertTo-Json -Depth 10

$initImg = @{
    _updatedAt = [int64](Get-Date -UFormat %s)
    days       = @()
} | ConvertTo-Json -Depth 10

$initDataList = @($initMain, $initDetail, $initImg)
$blobLabels   = @("主数据", "详情", "图片")
$blobIds      = @()
$apiUrl       = "https://jsonblob.com/api/jsonBlob"

for ($i = 0; $i -lt 3; $i++) {
    try {
        $body = $initDataList[$i]
        $resp = Invoke-WebRequest -Uri $apiUrl -Method POST `
                -Headers @{ "Content-Type" = "application/json" } `
                -Body $body -UseBasicParsing -TimeoutSec 30

        $location = $resp.Headers.Location
        if (-not $location) { $location = $resp.Headers['Location'] }
        if (-not $location) { $location = $resp.Headers['Content-Location'] }

        if ($location) {
            $blobId = ($location -split '/')[-1]
            $blobIds += $blobId
            Write-Ok "  $($blobLabels[$i]) 容器：$blobId"
        } else {
            Write-Err "  $($blobLabels[$i]) 容器创建失败：未返回 ID"
            Start-Sleep -Seconds 3
            exit 1
        }
    } catch {
        Write-Err "  $($blobLabels[$i]) 容器创建失败：$($_.Exception.Message)"
        Write-Warn2 "  请检查网络连接后重试。"
        Start-Sleep -Seconds 3
        exit 1
    }
    Start-Sleep -Milliseconds 300
}

# ---------- 4. 生成新文件名 ----------
$invalid = [System.IO.Path]::GetInvalidFileNameChars() -join ''
$safeName = $JourneyName -replace "[$([regex]::Escape($invalid))]", ''
$safeName = $safeName -replace '\s+', ' '
$newFileName = "$safeName.html"
$newFilePath = Join-Path $ProjectRoot $newFileName

if ($newFileName -eq $SourceFile) {
    $newFileName = "${safeName}_新.html"
    $newFilePath = Join-Path $ProjectRoot $newFileName
}

# ---------- 5. 复制源文件 ----------
Write-Step "正在生成新文件：$newFileName"
Copy-Item $sourcePath $newFilePath -Force

# ---------- 6. 替换配置 ----------
Write-Step "正在替换云端配置与旅程名称..."

$content = Get-Content $newFilePath -Raw -Encoding UTF8

$content = $content -replace "'019fa256-0006-79a8-b2d2-53a37d5653dc'", "'$($blobIds[0])'"
$content = $content -replace "'019fa256-6a30-751d-8a02-41678474be3d'", "'$($blobIds[1])'"
$content = $content -replace "'019fa256-6bf0-74ec-b9ac-5b927908a138'", "'$($blobIds[2])'"

# 替换旅程名称（JS变量、HTML标题、meta描述三处）
$safeNameForReplace = $JourneyName -replace '\$', '$$$$' -replace '\[', '$[' -replace '\]', '$]'
$content = $content -replace "tripName:\s*'[^']*'", "tripName: '$safeNameForReplace'"
$content = $content -replace "<title>[^<]*</title>", "<title>$safeNameForReplace</title>"
$content = $content -replace 'content="[^"]*行程规划"', "content=`"$safeNameForReplace 行程规划`""

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($newFilePath, $content, $utf8NoBom)

# ---------- 7. 完成汇总 ----------
Write-Host ""
Write-Host "==================================" -ForegroundColor Green
Write-Ok "新旅程创建成功！"
Write-Host "==================================" -ForegroundColor Green
Write-Host ""
Write-Host "  旅程名称：$JourneyName" -ForegroundColor Yellow
Write-Host "  文件位置：$newFilePath" -ForegroundColor Yellow
Write-Host ""
Write-Host "  云端容器（请妥善保存）：" -ForegroundColor Yellow
Write-Host "    主数据：$($blobIds[0])"
Write-Host "    详情  ：$($blobIds[1])"
Write-Host "    图片  ：$($blobIds[2])"
Write-Host ""
Write-Warn2 "下一步操作："
Write-Warn2 "  1. 双击打开新文件 $newFileName 即可使用"
Write-Warn2 "  2. 首次打开会加载空旅程，通过界面编辑添加 POI"
Write-Warn2 "  3. 如需部署到手机，将此 HTML 文件上传到 CloudBase 静态托管即可"
Write-Host ""

Write-Host "是否立即打开新文件？（输入 y 打开，其他键退出）" -ForegroundColor White
$open = [Console]::In.ReadLine()
if ($open -and ($open.Trim() -eq 'y' -or $open.Trim() -eq 'Y')) {
    Start-Process $newFilePath
}
