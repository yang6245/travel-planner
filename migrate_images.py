# -*- coding: utf-8 -*-
"""
行程图片外链化迁移脚本（一次性）
- 读取云端 trip.json（含 base64 内嵌大图）
- 解码 base64 → Pillow 重压缩（1280px / JPEG 0.82 / 白底去透明）
- 保存为 images/<行程目录>/dXpX.jpg 独立文件
- trip.json 中 img/cover 改写为 jsDelivr CDN URL
- 输出目录结构：out/data/<行程>/trip.json + out/images/<行程>/*.jpg（可直接 git add 或 API 推送）
"""
import json, os, re, base64, io, sys
from PIL import Image

TRIP_DIR = 'MiletoPuzhehei'                      # 与 dataPath 目录名一致
SRC = 'F:/开发者程序/旅游规划小程序/data_backup/_cloud_current.json'
OUT = 'F:/开发者程序/旅游规划小程序/_migrate_out'
CDN = 'https://cdn.jsdelivr.net/gh/yang6245/travel-planner@main/images/'

MAX_W = 1280
QUALITY = 82

def process_b64(b64: str, rel_path: str) -> str:
    """base64 → 重压缩 → 落盘，返回 CDN URL"""
    raw = base64.b64decode(b64.split(',')[1])
    img = Image.open(io.BytesIO(raw))
    # 统一转 RGB（PNG/WebP 透明图铺白底）
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    # 等比缩放到最大宽度
    if img.width > MAX_W:
        h = round(img.height * MAX_W / img.width)
        img = img.resize((MAX_W, h), Image.LANCZOS)
    out_path = os.path.join(OUT, rel_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, 'JPEG', quality=QUALITY, optimize=True)
    return CDN + rel_path.replace('images/', '').replace('\\', '/')

data = json.load(open(SRC, encoding='utf-8'))
migrated = 0
for di, day in enumerate(data.get('days') or []):
    cover = day.get('cover') or ''
    if cover.startswith('data:image'):
        rel = f'images/{TRIP_DIR}/d{di+1}_cover.jpg'
        day['cover'] = process_b64(cover, rel)
        migrated += 1
        print(f"  [封面] DAY{day.get('day')} -> {rel}")
    for pi, p in enumerate(day.get('pois') or []):
        i = p.get('img') or ''
        if i.startswith('data:image'):
            rel = f'images/{TRIP_DIR}/d{di+1}p{pi+1}.jpg'
            p['img'] = process_b64(i, rel)
            migrated += 1
            print(f"  [POI] d{di+1}p{pi+1} {p.get('name','?')[:12]} -> {rel}")

# 标记为新版本（触发观看端轮询刷新）
import time
data['_updatedAt'] = int(time.time() * 1000)

# 输出 URL 版 trip.json（紧凑格式）
trip_out = os.path.join(OUT, 'data', TRIP_DIR, 'trip.json')
os.makedirs(os.path.dirname(trip_out), exist_ok=True)
with open(trip_out, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

# 统计输出
img_dir = os.path.join(OUT, 'images', TRIP_DIR)
total_img = sum(os.path.getsize(os.path.join(img_dir, x)) for x in os.listdir(img_dir)) if os.path.isdir(img_dir) else 0
new_size = os.path.getsize(trip_out)
print(f"\n迁移完成: 图片 {migrated} 张, 图片总大小 {total_img/1024:.0f} KB")
print(f"新 trip.json: {new_size/1024:.1f} KB (原 7.34MB)")
print(f"输出目录: {OUT}")
