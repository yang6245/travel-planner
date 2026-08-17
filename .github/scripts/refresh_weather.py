#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Q3 服务端自动化（GitHub Actions 定时工作流）— 天气刷新 + AI 旅途提示重生成。

逻辑（与交接说明 4.3 对齐）：
  1. 对每个 data/<行程名>/trip.json：
     读 data/<行程名>/last_access.json（前端有 Token 者打开时心跳写入）；
     now - lastAccess > 24h 则跳过该行程（闲置软关闭，下次打开自动恢复）。
  2. 拉天气（高德优先 / Open-Meteo 兜底补齐缺失日期），生成 weatherSnap（与前端 computeWeatherSnap 同构）。
  3. 与 trip.json 内 aiTips.weatherSnap 比对（analyzeChange 复刻：温差/降水分级/极端天气，按敏感档位阈值）。
  4. 有变化 且 now - aiTips.updatedAt > 6h → 调 fc_proxy genTips 重新生成旅途提示（单一入口），更新 aiTips。
  5. 更新 aiTips.updatedAt、weatherUpdatedAt，写回 GitHub（Contents API，内容用本地文件、sha 走 API）。

环境变量（仓库 secrets）：
  GH_TOKEN        必填：写入 PAT（最小权限，仅授权本仓库）
  AI_API_KEY      服务端共享 AI key（genTips 用）
  AI_PROVIDER     可选：deepseek / dashscope / 自定义（默认 deepseek）
  AI_MODEL        可选：模型名
  AI_BASE_URL     可选：自定义 OpenAI 兼容端点
  FC_PROXY_URL    可选：阿里云 FC 代理地址；未配置则跳过 AI 重生成（只刷天气）
  AMAP_KEY        可选：高德 key；未配置时自动从仓库 index.html / MiletoPuzhehei.html 提取（公开常量）

依赖：仅标准库。
"""
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

OWNER = os.environ.get('GH_OWNER', 'yang6245')
REPO = os.environ.get('GH_REPO', 'travel-planner')
BRANCH = 'main'
API = 'https://api.github.com'

GH_TOKEN = os.environ.get('GH_TOKEN', '')
AMAP_KEY = os.environ.get('AMAP_KEY', '')
FC_URL = os.environ.get('FC_PROXY_URL', '').strip()
AI_KEY = os.environ.get('AI_API_KEY', '')
AI_PROVIDER = os.environ.get('AI_PROVIDER', 'deepseek')
AI_MODEL = os.environ.get('AI_MODEL', '')
AI_BASE = os.environ.get('AI_BASE_URL', '')

IDLE_HOURS = 24                      # lastAccess 闲置阈值
REGEN_MIN_INTERVAL_MS = 6 * 3600 * 1000   # 重生成最小间隔 6h
AMAP_QPS_GAP = 0.35                  # 高德串行队列间隔（个人 key QPS≈3）
WMO = {0: '晴', 1: '晴间多云', 2: '多云', 3: '阴',
       45: '雾', 48: '雾凇',
       51: '小毛毛雨', 53: '毛毛雨', 55: '大毛毛雨',
       56: '小冻雨', 57: '大冻雨',
       61: '小雨', 63: '中雨', 65: '大雨',
       66: '小冻雨', 67: '大冻雨',
       71: '小雪', 73: '中雪', 75: '大雪', 77: '米雪',
       80: '小阵雨', 81: '中阵雨', 82: '大阵雨',
       85: '小阵雪', 86: '大阵雪',
       95: '雷暴', 96: '雷暴伴冰雹', 99: '强雷暴伴冰雹'}
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


def http(url, data=None, method=None, headers=None, timeout=25):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def gh_headers():
    return {'Authorization': 'Bearer ' + GH_TOKEN,
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json'}


def gh_get_sha(path):
    """取文件 sha（contents API 对 >1MB 文件 content 为空但 sha 可用）。"""
    url = '%s/repos/%s/%s/contents/%s?ref=%s' % (
        API, OWNER, REPO, urllib.parse.quote(path, safe='/'), BRANCH)
    try:
        data = json.loads(http(url, headers=gh_headers()))
        return data.get('sha', '')
    except Exception as e:
        log('读取 sha 失败 %s: %s' % (path, e))
        return ''


def gh_put(path, obj, sha=''):
    b64 = base64.b64encode(json.dumps(obj, ensure_ascii=False).encode('utf-8')).decode('utf-8')
    body = {'message': 'auto refresh ' + time.strftime('%Y-%m-%d %H:%M:%S'),
            'content': b64, 'branch': BRANCH}
    if sha:
        body['sha'] = sha
    url = '%s/repos/%s/%s/contents/%s' % (
        API, OWNER, REPO, urllib.parse.quote(path, safe='/'))
    http(url, data=json.dumps(body).encode('utf-8'), method='PUT', headers=gh_headers())
    return True


# ---------- 天气 ----------
def get_display_weather(text):
    """雷暴修正（与前端 getDisplayWeather 一致）：Open-Meteo 95/96/99 降级为雷阵雨类。"""
    if not text:
        return ''
    s = str(text)
    if s in ('95', '96', '99', '雷暴', '雷暴伴冰雹', '强雷暴伴冰雹'):
        return '雷阵雨'
    return s


def amap_regeo(lng, lat):
    """逆地理编码取 adcode（高德，串行队列外由调用方控制节奏）。"""
    url = ('https://restapi.amap.com/v3/geocode/regeo?location=%s,%s&key=%s'
           '&extensions=base&output=JSON&radius=200&poitype=&roadlevel=0' % (lng, lat, AMAP_KEY))
    try:
        data = json.loads(http(url, timeout=10))
        if data.get('status') == '1' and data.get('regeocode'):
            return data['regeocode'].get('addressComponent', {}).get('adcode', '')
    except Exception:
        pass
    return ''


def amap_weather(adcode):
    """高德天气（4 天，主源）。"""
    url = 'https://restapi.amap.com/v3/weather/weatherInfo?key=%s&city=%s&extensions=all' % (AMAP_KEY, adcode)
    try:
        data = json.loads(http(url, timeout=10))
        if data.get('status') == '1' and data.get('forecasts'):
            casts = data['forecasts'][0].get('casts') or []
            out = []
            for c in casts:
                out.append({
                    'date': c.get('date', ''),
                    'dayWeather': get_display_weather(c.get('dayweather') or ''),
                    'nightWeather': get_display_weather(c.get('nightweather') or ''),
                    'dayTemp': float(c.get('daytemp') or 0),
                    'nightTemp': float(c.get('nighttemp') or 0),
                    'dayWind': (c.get('daywind') or '') + ((' ' + c.get('daypower') + '级') if c.get('daypower') else '')
                })
            return out
    except Exception:
        pass
    return []


def open_meteo(lng, lat):
    """Open-Meteo（16 天，兜底补齐高德 4 天之外的日期）。"""
    url = ('https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s'
           '&daily=weathercode,temperature_2m_max,temperature_2m_min,windspeed_10m_max,winddirection_10m_dominant'
           '&timezone=Asia/Shanghai&forecast_days=16' % (lat, lng))
    try:
        data = json.loads(http(url, timeout=15))
        d = data.get('daily') or {}
        times = d.get('time') or []
        if not times:
            return []
        out = []
        for i, date in enumerate(times):
            code = d.get('weathercode', [0])[i]
            out.append({
                'date': date,
                'dayWeather': get_display_weather(WMO.get(code, '未知')),
                'nightWeather': get_display_weather(WMO.get(code, '未知')),
                'dayTemp': float(d.get('temperature_2m_max', [0])[i] or 0),
                'nightTemp': float(d.get('temperature_2m_min', [0])[i] or 0),
                'dayWind': ''
            })
        return out
    except Exception:
        return []


def merge_forecasts(primary, secondary):
    """高德优先，Open-Meteo 补缺失日期，按日期升序（与前端 mergeForecasts 一致）。"""
    m = {}
    for f in primary or []:
        if f.get('date'):
            m[f['date']] = f
    for f in secondary or []:
        if f.get('date') and f['date'] not in m:
            m[f['date']] = f
    return [m[k] for k in sorted(m.keys())]


def day_diff_days(date_str, now=None):
    """行程日期 - 今天（天数差）。"""
    try:
        now = now or time.localtime()
        today = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
        t = time.strptime(str(date_str), '%Y-%m-%d')
        d = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        return int(round((d - today) / 86400))
    except Exception:
        return 99


def tier_for_date(date_str):
    d = day_diff_days(date_str)
    if d in (0, 1):
        return 1
    if d in (-1, 2, 3):
        return 2
    return 3


def rain_level(dw, nw):
    s = (dw or '') + (nw or '')
    if re.search('中|大|暴', s) and re.search('雨|雪|雷', s):
        return 2
    if re.search('雨|雪|雷|阵|雹|霰', s):
        return 1
    return 0


def is_extreme_weather(dw, nw, win):
    """极端天气：冰雹/暴雪/浓雾/大雾/沙尘/霾/6级+大风。
    不匹配"雷"字——雷暴已由 get_display_weather 降级为"雷阵雨"（非极端），避免误报（22:54 修复）。"""
    s = (dw or '') + (nw or '')
    if re.search('雹|暴雪|浓雾|大雾|沙尘|霾', s):
        return True
    m = re.search(r'(\d+)\s*级', win or '')
    if m and int(m.group(1)) >= 6:
        return True
    return False


def analyze_change(old_f, new_f, tier):
    """复刻前端 analyzeChange：0=无 1=重大 2=特别重大。
    温差阈值（22:54 用户要求 3℃→5℃）：tier1(今明) 重大≥5 特别重大≥8；tier2 ≥8/12；tier3 ≥12/16"""
    severe_temp = [8, 12, 16][tier - 1]
    major_temp = [5, 8, 12][tier - 1]
    level, text = 0, ''
    d_t = max(abs(float(new_f.get('dt') or 0) - float(old_f.get('dt') or 0)),
              abs(float(new_f.get('nt') or 0) - float(old_f.get('nt') or 0)))
    if d_t >= severe_temp:
        level = 2
        text = '温差%d°C' % round(d_t)
    elif d_t >= major_temp:
        level = max(level, 1)
        text = '温差%d°C' % round(d_t)
    o_r = rain_level(old_f.get('dw'), old_f.get('nw'))
    n_r = rain_level(new_f.get('dw'), new_f.get('nw'))
    if o_r != n_r:
        if n_r == 2 or o_r == 2:
            level = 2
            text = (text + '；' if text else '') + '中大雨变化(%s→%s)' % (old_f.get('dw') or '', new_f.get('dw') or '')
        else:
            level = max(level, 1)
            text = (text + '；' if text else '') + '降水变化(%s→%s)' % (old_f.get('dw') or '', new_f.get('dw') or '')
    o_e = is_extreme_weather(old_f.get('dw'), old_f.get('nw'), old_f.get('win'))
    n_e = is_extreme_weather(new_f.get('dw'), new_f.get('nw'), new_f.get('win'))
    if o_e != n_e:
        level = 2
        text = (text + '；' if text else '') + '极端天气' + ('出现' if n_e else '消散')
    return level, text or (new_f.get('dw') or '天气变化')


def get_day_date(start_date, day_num):
    try:
        start = time.strptime(str(start_date or time.strftime('%Y-%m-%d')), '%Y-%m-%d')
        t = time.localtime(time.mktime(start) + (day_num - 1) * 86400)
        return time.strftime('%Y-%m-%d', t)
    except Exception:
        return ''


def extract_amap_key(root):
    """从 index.html / MiletoPuzhehei.html 提取高德 key（公开常量，避免额外 secret）。"""
    if AMAP_KEY:
        return AMAP_KEY
    for name in ('index.html', 'MiletoPuzhehei.html'):
        p = os.path.join(root, name)
        try:
            s = open(p, encoding='utf-8').read()
            m = re.search(r'AMAP_KEY\s*=\s*[\'"]([0-9a-f]{32})[\'"]', s)
            if m:
                return m.group(1)
        except Exception:
            continue
    return ''


def fetch_weather_snap(trip):
    """为行程生成天气快照 {date: {dw,nw,dt,nt,win}}（与前端 computeWeatherSnap 同构：
    POI 按天取第一个有坐标的，高德优先 + Open-Meteo 兜底补齐）。"""
    days = trip.get('days') or []
    start_date = trip.get('startDate') or ''
    # 收集去重坐标
    coords = []
    seen = set()
    for d in days:
        for p in (d.get('pois') or []):
            lng, lat = p.get('lng'), p.get('lat')
            if lng is None or lat is None:
                continue
            key = '%.4f,%.4f' % (float(lng), float(lat))
            if key not in seen:
                seen.add(key)
                coords.append((float(lng), float(lat)))
    if not coords:
        return {}
    # 逆地理编码（高德串行）
    adcode_map = {}
    for lng, lat in coords:
        ad = amap_regeo(lng, lat)
        if ad:
            adcode_map['%.4f,%.4f' % (lng, lat)] = ad
        time.sleep(AMAP_QPS_GAP)
    # 按 adcode 去重拉高德（串行）
    city_cache = {}
    for coord, ad in adcode_map.items():
        if ad in city_cache:
            continue
        city_cache[ad] = amap_weather(ad)
        time.sleep(AMAP_QPS_GAP)
    # 每个坐标：高德 + Open-Meteo 合并
    coord_forecast = {}
    for lng, lat in coords:
        key = '%.4f,%.4f' % (lng, lat)
        ad = adcode_map.get(key, '')
        om = open_meteo(lng, lat)
        coord_forecast[key] = merge_forecasts(city_cache.get(ad, []), om)
    # 生成快照：每天第一个有天气的 POI
    snap = {}
    for d in days:
        date = get_day_date(start_date, d.get('day') or 1)
        if not date:
            continue
        f = None
        for p in (d.get('pois') or []):
            lng, lat = p.get('lng'), p.get('lat')
            if lng is None or lat is None:
                continue
            key = '%.4f,%.4f' % (float(lng), float(lat))
            for fc in coord_forecast.get(key, []):
                if fc.get('date') == date:
                    f = fc
                    break
            if f:
                break
        if f:
            snap[date] = {
                'dw': f.get('dayWeather') or '',
                'nw': f.get('nightWeather') or '',
                'dt': float(f.get('dayTemp') or 0),
                'nt': float(f.get('nightTemp') or 0),
                'win': f.get('dayWind') or ''
            }
    return snap


def gen_tips_via_fc(trip, risk_reasons, changed_days=None):
    """调 fc_proxy genTips 重生成（单一入口）；无 FC 配置返回 None（只刷天气）。
    changed_days 为 None 时全量生成所有天，否则只生成指定天 + 总览（限流优化：减少 N+1 连发）。"""
    if not FC_URL or not AI_KEY:
        log('未配置 FC_PROXY_URL 或 AI_API_KEY，跳过 AI 重生成（仅刷新天气）')
        return None
    summary = {
        'tripName': trip.get('tripName', ''),
        'subtitle': trip.get('subtitle', ''),
        'days': [{
            'day': d.get('day'),
            'title': d.get('title', ''),
            'desc': d.get('desc', ''),
            'pois': [{'time': p.get('time', ''), 'type': p.get('type', ''),
                      'name': p.get('name', ''), 'desc': p.get('desc', '')}
                     for p in (d.get('pois') or [])]
        } for d in (trip.get('days') or [])]
    }
    body = {
        'action': 'genTips', 'provider': AI_PROVIDER, 'apiKey': AI_KEY,
        'model': AI_MODEL, 'baseUrl': AI_BASE,
        'kind': 'day', 'day': 1, 'risk': None, 'trip': summary
    }
    results = {'overview': None, 'days': {}}
    all_days = [d.get('day') for d in (trip.get('days') or []) if d.get('day')]
    day_list = changed_days if changed_days is not None else all_days

    def call(kind, day, retry=0):
        body['kind'] = kind
        body['day'] = day or 0
        body['risk'] = (risk_reasons or None) if kind == 'overview' else None
        try:
            resp = json.loads(http(FC_URL, data=json.dumps(body).encode('utf-8'),
                                   method='POST', timeout=60,
                                   headers={'Content-Type': 'application/json'}))
            tip = resp.get('tip')
            if isinstance(tip, dict):
                return tip
            # 服务端可能返回 error 字段（如 429 限流文案），按可重试处理
            err = str(resp.get('error') or '')
            if retry < 3 and ('429' in err or '超时' in err or 'timeout' in err):
                backoff = 2.5 * (2 ** retry)   # 指数退避 2.5s/5s/10s
                log('genTips(%s) 限流/超时 %s，%.1fs 后重试(%d/3)' % (kind, err, backoff, retry + 1))
                time.sleep(backoff)
                return call(kind, day, retry + 1)
            return None
        except Exception as e:
            msg = str(e)
            if retry < 3 and ('429' in msg or '超时' in msg or 'timeout' in msg or '500' in msg or '502' in msg or '503' in msg):
                backoff = 2.5 * (2 ** retry)
                log('genTips(%s) 异常 %s，%.1fs 后重试(%d/3)' % (kind, msg, backoff, retry + 1))
                time.sleep(backoff)
                return call(kind, day, retry + 1)
            log('genTips(%s) 失败: %s' % (kind, msg))
            return None

    for day in day_list:
        t = call('day', day)
        if t:
            results['days'][str(day)] = t
        time.sleep(2.0)   # 串行间隔 2s（原 0.4s），降低免费模型 RPM 限流概率
    results['overview'] = call('overview', 0)
    if not results['overview'] and not results['days']:
        return None
    return results


def process_trip(name, root):
    """处理单个行程目录。返回 True 表示有更新。"""
    trip_path = os.path.join(root, 'data', name, 'trip.json')
    access_path = os.path.join(root, 'data', name, 'last_access.json')
    if not os.path.exists(trip_path):
        return False
    try:
        trip = json.load(open(trip_path, encoding='utf-8'))
    except Exception as e:
        log('读取行程 %s 失败: %s' % (name, e))
        return False
    if not trip.get('days'):
        return False
    # 过期判定：行程全部日期 < 今天（已结束），直接跳过、不调任何高德 API（根除对已结束行程的空耗）
    trip_end = ''
    for d in (trip.get('days') or []):
        dt = get_day_date(trip.get('startDate') or '', d.get('day') or 1)
        if dt and dt > trip_end:
            trip_end = dt
    if trip_end and day_diff_days(trip_end) < 0:
        log('行程 %s 已结束（最后一天 %s < 今天），跳过天气刷新（省高德配额）' % (name, trip_end))
        return False
    # 闲置关闭：last_access 缺失（游客态/未用管理员态打开）或 idle>24h 均跳过
    # 修复：原逻辑仅在文件存在时判定，缺失则默认全量拉，造成游客态永不触发的漏洞
    if not os.path.exists(access_path):
        log('行程 %s 无 last_access.json（游客态/未用管理员态打开），按闲置跳过' % name)
        return False
    try:
        la = json.load(open(access_path, encoding='utf-8')).get('lastAccess') or 0
        idle = time.time() * 1000 - la
        if idle > IDLE_HOURS * 3600 * 1000:
            log('行程 %s 闲置 %.1fh > %dh，跳过（软关闭）' % (name, idle / 3600000.0, IDLE_HOURS))
            return False
    except Exception:
        pass
    # 拉天气生成快照
    new_snap = fetch_weather_snap(trip)
    if not new_snap:
        log('行程 %s 天气拉取失败，保留上次缓存，跳过' % name)
        return False
    old_snap = (trip.get('aiTips') or {}).get('weatherSnap') or {}
    old_tips = trip.get('aiTips') or {}
    last_upd = old_tips.get('updatedAt') or 0
    # 对比变化
    reasons = []
    changed = False
    changed_dates = set()
    for date, nf in new_snap.items():
        of = old_snap.get(date)
        if not of:
            continue
        level, text = analyze_change(of, nf, tier_for_date(date))
        if level >= 1:
            changed = True
            changed_dates.add(date)
            reasons.append('%s：%s' % (date, text))
    # 生成风险（与前端一致：中大雨/极端天气）
    risk = None
    s_list = []
    for date, f in new_snap.items():
        if rain_level(f.get('dw'), f.get('nw')) == 2 or is_extreme_weather(f.get('dw'), f.get('nw'), f.get('win')):
            s_list.append('%s（%s %s°C）' % (date, f.get('dw') or '', round(f.get('dt') or 0)))
    if s_list:
        risk = {'text': '；'.join(s_list), 'updatedAt': int(time.time() * 1000)}
    # 重生成条件：有变化 且 距上次 >6h（只重生成有变化的那几天 + 总览，未变化的沿用旧提示，降低免费模型限流）
    ai_tips = dict(old_tips)
    if changed and (time.time() * 1000 - last_upd) > REGEN_MIN_INTERVAL_MS:
        changed_days = []
        if changed_dates:
            for d in (trip.get('days') or []):
                dt = get_day_date(trip.get('startDate') or '', d.get('day') or 1)
                if dt in changed_dates:
                    changed_days.append(d.get('day'))
        log('行程 %s 天气变化 %d 天：%s，距上次 %dh，触发 AI 重生成（仅重生成 %s）' % (
            name, len(changed_days), '；'.join(reasons) if reasons else '有变化',
            (time.time() * 1000 - last_upd) // 3600000,
            ('DAY' + str(changed_days)) if changed_days else '全部天'))
        gen = gen_tips_via_fc(trip, reasons, changed_days or None)
        if gen:
            old_days = (old_tips.get('days') or {})
            merged_days = dict(old_days)
            for k, v in (gen['days'] or {}).items():
                merged_days[str(k)] = v
            ai_tips = {
                'overview': gen['overview'] or (old_tips.get('overview') or ''),
                'days': merged_days,
                'weatherSnap': new_snap,
                'risk': risk,
                'updatedAt': int(time.time() * 1000)
            }
        else:
            ai_tips['weatherSnap'] = new_snap
    else:
        ai_tips['weatherSnap'] = new_snap
    # 写回
    trip['aiTips'] = ai_tips
    trip['weatherUpdatedAt'] = int(time.time() * 1000)
    sha = gh_get_sha('data/%s/trip.json' % name)
    try:
        gh_put('data/%s/trip.json' % name, trip, sha)
        log('行程 %s 已更新（weatherUpdatedAt=%s%s）' % (
            name, trip['weatherUpdatedAt'],
            '，AI 已重生成' if ai_tips.get('updatedAt', 0) > (old_tips.get('updatedAt') or 0) else ''))
        return True
    except Exception as e:
        log('行程 %s 写回失败: %s' % (name, e))
        return False


def main():
    if not GH_TOKEN:
        log('缺少 GH_TOKEN，退出')
        return 1
    root = REPO_ROOT
    key = extract_amap_key(root)
    if not key:
        log('未找到高德 key（AMAP_KEY 环境变量或 index.html 中均无），退出')
        return 1
    globals()['AMAP_KEY'] = key
    log('高德 key 就绪（%s），FC=%s，AI=%s' % ('环境变量' if os.environ.get('AMAP_KEY') else 'HTML 提取',
                                             FC_URL or '未配置', AI_KEY and '已配置' or '未配置'))
    data_dir = os.path.join(root, 'data')
    if not os.path.isdir(data_dir):
        log('本地 data/ 目录不存在，退出')
        return 1
    names = [n for n in os.listdir(data_dir)
             if os.path.isdir(os.path.join(data_dir, n)) and
             os.path.exists(os.path.join(data_dir, n, 'trip.json'))]
    if not names:
        log('未发现任何行程数据（data/*/trip.json）')
        return 0
    updated = False
    for name in sorted(names):
        try:
            if process_trip(name, root):
                updated = True
        except Exception as e:
            log('行程 %s 处理异常: %s' % (name, e))
    log('完成。%s' % ('有更新' if updated else '无变化'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
