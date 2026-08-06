<#
.SYNOPSIS
  一键创建新旅程脚本（GitHub 仓库存储版）
.DESCRIPTION
  1. 复制源文件 index.html 作为模板
  2. 替换 GitHub 存储路径（dataPath）和旅程名称
  3. 生成独立的新旅程 HTML 文件（每个旅程 = 一个独立网站）
  4. 数据存储在 GitHub 仓库 travel-planner 的不同子目录中，互不干扰
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
    Write-Host "   一键创建新旅程（GitHub 版）" -ForegroundColor Cyan
    Write-Host "==================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "请输入新旅程名称（例如：川西环线5日游）" -ForegroundColor White
    Write-Host "输入后按回车键确认：" -ForegroundColor White
    $JourneyName = [Console]::In.ReadLine()
    if ($JourneyName) { $JourneyName = $JourneyName.Trim() }
}

if (-not $JourneyName -or $JourneyName -eq "") {
    Write-Err "旅程名称不能为空，脚本已退出。"
    Start-Sleep -Seconds 3
    exit 1
}

# ---------- 2. 校验源文件 ----------
$SourceFile = "index.html"
$sourcePath = Join-Path $ProjectRoot $SourceFile
if (-not (Test-Path $sourcePath)) {
    Write-Err "源文件不存在：$sourcePath"
    Start-Sleep -Seconds 3
    exit 1
}

Write-Step "源文件：$SourceFile"

# ---------- 3. 生成新文件名 ----------
$invalid = [System.IO.Path]::GetInvalidFileNameChars() -join ''
$safeName = $JourneyName -replace "[$([regex]::Escape($invalid))]", ''
$safeName = $safeName -replace '\s+', ' '
$newFileName = "$safeName.html"
$newFilePath = Join-Path $ProjectRoot $newFileName

if ($newFileName -eq $SourceFile) {
    $newFileName = "${safeName}_新.html"
    $newFilePath = Join-Path $ProjectRoot $newFileName
}

# ---------- 4. 复制源文件 ----------
Write-Step "正在生成新文件：$newFileName"
Copy-Item $sourcePath $newFilePath -Force

# ---------- 5. 替换配置 ----------
Write-Step "正在替换存储路径与旅程名称..."

$content = Get-Content $newFilePath -Raw -Encoding UTF8

# 替换 dataPath（GitHub 仓库中的存储路径）
$content = $content -replace "dataPath:\s*'data/[^']*'", "dataPath: 'data/$safeName/trip.json'"

# 替换旅程名称（JS变量、HTML标题、meta描述、hero区域四处）
$safeNameForReplace = $JourneyName -replace '\$', '$$$$' -replace '\[', '$[' -replace '\]', '$]'
$content = $content -replace "tripName:\s*'[^']*'", "tripName: '$safeNameForReplace'"
$content = $content -replace "<title>[^<]*</title>", "<title>$safeNameForReplace</title>"
$content = $content -replace 'content="[^"]*行程规划"', "content=`"$safeNameForReplace 行程规划`""
# 替换 hero 区域静态标题（避免新文件显示模板的"我的行程"）
$content = $content -replace '<h1 id="tripTitle">[^<]*</h1>', "<h1 id=`"tripTitle`">$safeNameForReplace</h1>"

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($newFilePath, $content, $utf8NoBom)

# ---------- 6. 完成汇总 ----------
Write-Host ""
Write-Host "==================================" -ForegroundColor Green
Write-Ok "新旅程创建成功！"
Write-Host "==================================" -ForegroundColor Green
Write-Host ""
Write-Host "  旅程名称：$JourneyName" -ForegroundColor Yellow
Write-Host "  文件位置：$newFilePath" -ForegroundColor Yellow
Write-Host "  存储路径：GitHub 仓库 data/$safeName/trip.json" -ForegroundColor Yellow
Write-Host ""
Write-Warn2 "下一步操作："
Write-Warn2 "  1. 双击打开新文件 $newFileName"
Write-Warn2 "  2. 点击「编辑」按钮，输入管理员密码（默认 88888888）"
Write-Warn2 "  3. 点击紫色「同步设置」按钮，输入 GitHub Token（每个文件首次需输入一次）"
Write-Warn2 "  4. 退出编辑模式后重新进入，编辑行程内容并「保存」，完成首次数据上传"
Write-Warn2 "  5. 如需网页访问，将此 HTML 文件上传到 GitHub 仓库 travel-planner"
Write-Warn2 "     访问地址：https://yang6245.github.io/travel-planner/$newFileName"
Write-Host ""
Write-Warn2 "提示：普通访客打开网址无需 Token，可直接只读查看行程"
Write-Host ""

Write-Host "是否立即打开新文件？（输入 y 打开，其他键退出）" -ForegroundColor White
$open = [Console]::In.ReadLine()
if ($open -and ($open.Trim() -eq 'y' -or $open.Trim() -eq 'Y')) {
    Start-Process $newFilePath
}
