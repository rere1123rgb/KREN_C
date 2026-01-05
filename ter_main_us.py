import requests
import json
import os
import time
import logging
import threading
import queue
import math
import traceback
import sys
# tkinter 관련 import 모두 제거
from datetime import datetime, timedelta
import pytz
import yfinance as yf
import pandas as pd

# [B] 절대 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================
# [1. 설정 및 상수]
# ==========================================
MODE = "US_REAL"
SECRETS_FILE = os.path.join(BASE_DIR, "secrets.json")
STATUS_FILE = os.path.join(BASE_DIR, "status_us.json")
LOG_FILE_NAME = os.path.join(BASE_DIR, f"log_us_{datetime.now().strftime('%Y%m%d')}.txt")
TOKEN_FILE = os.path.join(BASE_DIR, f"token_{MODE}.json")

# [수정됨] 타겟 종목 및 거래소 정보 (문서 기준 NASD, AMEX)
TARGETS = [
    {"symbol": "TQQQ", "exch": "NASD"}, # 나스닥은 NAS가 아니라 NASD
    {"symbol": "SOXL", "exch": "AMEX"}  # 아멕스/Arca는 AMS가 아니라 AMEX
]

# 로깅 설정
logging.basicConfig(
    filename=LOG_FILE_NAME,
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S'
)

# GUI용 큐
log_queue = queue.Queue()

# ==========================================
# [2. 유틸리티]
# ==========================================
def print_log(msg):
    # Termux에서는 print로 직접 출력
    print(msg) 
    logging.info(msg)

def send_discord(msg):
    try:
        with open(SECRETS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            url = data.get(MODE, {}).get("DISCORD_WEBHOOK") or data.get("DISCORD_WEBHOOK")
        if url: requests.post(url, json={"content": msg})
    except: pass

def get_market_status():
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)
    
    # 요일 체크 (0:월 ~ 4:금, 5:토, 6:일)
    if now_ny.weekday() >= 5:
        return False, now_ny.strftime("%H:%M") + " (주말)"
    
    # 시간 체크
    current_time = now_ny.strftime("%H:%M")
    is_open = "09:30" <= current_time < "16:00"
    return is_open, current_time

# ==========================================
# [3. 상태 관리]
# ==========================================
class StatusManager:
    def __init__(self):
        self.file = STATUS_FILE
        self.lock = threading.Lock()
        self.data = self._load()
        self.pending_buys = {} 

    def _load(self):
        if os.path.exists(self.file):
            try:
                with open(self.file, 'r') as f: return json.load(f)
            except: pass
        return {"phase_a_done": False, "max_profit": {}, "ignore_list": {}}

    def _save(self):
        try:
            with open(self.file, 'w') as f: json.dump(self.data, f, indent=4)
        except: pass

    def record_pending_buy(self, symbol, qty, current_qty):
        with self.lock:
            self.pending_buys[symbol] = {
                'qty': qty,
                'time': time.time(),
                'initial_qty': current_qty 
            }
            print_log(f"📝 [가상잔고] {symbol} +{qty}주 기록 (API 반영 대기)")

    def get_virtual_qty(self, symbol, current_qty):
        with self.lock:
            if symbol not in self.pending_buys:
                return current_qty
            
            info = self.pending_buys[symbol]
            if current_qty > info['initial_qty']:
                print_log(f"✅ [동기화완료] {symbol} 잔고 업데이트 확인.")
                del self.pending_buys[symbol]
                return current_qty
            
            if time.time() - info['time'] > 600:
                print_log(f"⚠️ [타임아웃] {symbol} 잔고 미반영 -> 가상잔고 삭제")
                del self.pending_buys[symbol]
                return current_qty
            
            return current_qty + info['qty']

    def get_max_profit(self, symbol):
        with self.lock: return self.data["max_profit"].get(symbol, 0.0)

    def update_max_profit(self, symbol, rate):
        with self.lock:
            if "max_profit" not in self.data: self.data["max_profit"] = {}
            if rate > self.data["max_profit"].get(symbol, -999.0):
                self.data["max_profit"][symbol] = rate
                self._save()
    
    def reset_max_profit(self, symbol):
        with self.lock:
            if "max_profit" in self.data and symbol in self.data["max_profit"]:
                del self.data["max_profit"][symbol]
                self._save()
            print_log(f"🔄 [{symbol}] 평단 변화 감지 -> 최고 수익률 리셋")

    def set_phase_a_done(self, done=True):
        with self.lock:
            self.data["phase_a_done"] = done
            self._save()
    
    def reset_daily(self):
        with self.lock:
            self.data["phase_a_done"] = False
            self.data["max_profit"] = {}
            self.data["ignore_list"] = {}
            self.pending_buys = {}
            self._save()

    def set_ignore_sync(self, symbol, duration=3600):
        with self.lock:
            if "ignore_list" not in self.data: self.data["ignore_list"] = {}
            self.data["ignore_list"][symbol] = time.time() + duration
            self._save()
            print_log(f"🛡️ [동기화] {symbol} {int(duration/60)}분간 잔고 동기화 제외")

    def is_sync_ignored(self, symbol):
        with self.lock:
            expire = self.data.get("ignore_list", {}).get(symbol, 0)
            if time.time() < expire: return True
            return False

status_mgr = StatusManager()

# ==========================================
# [4. 데이터 Provider]
# ==========================================
class DataProvider:
    _cache = {}
    _cache_duration = 300  # 5분 캐싱

    @staticmethod
    def get_current_price(symbol):
        # 3회 재시도
        for attempt in range(3):
            try:
                ticker = yf.Ticker(symbol)
                
                # 1. 실시간 가격 시도 (fast_info)
                price = ticker.fast_info.get('last_price', None)
                if price and price > 0: 
                    return float(price)
                
                # 2. 실패 시(주말 등), 최근 종가 가져오기 (history)
                hist = ticker.history(period="1d")
                if not hist.empty:
                    close_price = hist['Close'].iloc[-1]
                    return float(close_price)
                    
            except: 
                time.sleep(0.5)
        
        return None

    @classmethod
    def get_daily_history(cls, symbol, days=130):
        now = time.time()
        if symbol in cls._cache:
            cached_data, cached_time = cls._cache[symbol]
            if (now - cached_time < cls._cache_duration) and (len(cached_data) >= days):
                return cached_data

        for attempt in range(3):
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="1y") 
                
                if hist is not None and not hist.empty:
                    if len(hist) < days:
                         print_log(f"⚠️ [Data] {symbol} 데이터 부족 (확보:{len(hist)} < 필요:{days})")
                         return None
                    
                    cls._cache[symbol] = (hist, now)
                    return hist 
            except Exception as e:
                if attempt == 2: print_log(f"⚠️ [Data] {symbol} 조회 에러: {e}")
                time.sleep(1)
        
        return None

# ==========================================
# [5. API 클래스]
# ==========================================
class KisUS:
    def __init__(self):
        with open(SECRETS_FILE, 'r') as f:
            self.cfg = json.load(f)[MODE]
        self.base_url = self.cfg['URL_BASE']
        self.token = None
        self.token_file = TOKEN_FILE
        self.get_access_token()

    def get_access_token(self):
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, 'r') as f:
                    data = json.load(f)
                saved = datetime.fromisoformat(data['timestamp'])
                if datetime.now() < saved + timedelta(hours=23):
                    self.token = data['access_token']
                    print_log(f"🔑 기존 토큰 사용 (만료: {saved + timedelta(hours=24)})")
                    return
            except: pass
        
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.cfg['APP_KEY'],
            "appsecret": self.cfg['APP_SECRET']
        }
        try:
            res = requests.post(url, json=body).json()
            if 'access_token' in res:
                self.token = res['access_token']
                with open(self.token_file, 'w') as f:
                    json.dump({"access_token": self.token, "timestamp": datetime.now().isoformat()}, f)
                print_log("🔑 새 토큰 발급 완료")
            else:
                print_log(f"❌ 토큰 발급 응답 오류: {res}")
        except Exception as e:
            print_log(f"❌ 토큰 발급 실패: {e}")

    def get_header(self, tr_id):
        if not self.token: self.get_access_token()
        return {
            "authorization": f"Bearer {self.token}",
            "appkey": self.cfg['APP_KEY'],
            "appsecret": self.cfg['APP_SECRET'],
            "tr_id": tr_id,
            "content-type": "application/json"
        }

    def get_buyable_cash(self):
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        tr_id = "TTTS3007R" if "REAL" in MODE else "VTTS3007R"
        headers = self.get_header(tr_id) 
        params = {
            "CANO": self.cfg['CANO'], 
            "ACNT_PRDT_CD": self.cfg['ACNT_PRDT_CD'],
            "OVRS_EXCG_CD": "NASD",  # 나스닥 기준
            "OVRS_ORD_UNPR": "0", 
            "ITEM_CD": "TQQQ", 
            "TR_CRCY_CD": "USD"
        }
        try:
            res = requests.get(url, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    return float(data['output']['frcr_ord_psbl_amt1']) 
        except: pass
        return 0.0

    def get_balance(self):
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        tr_id = "TTTS3012R" if "REAL" in MODE else "VTTS3012R"
        headers = self.get_header(tr_id)
        params = {
            "CANO": self.cfg['CANO'], 
            "ACNT_PRDT_CD": self.cfg['ACNT_PRDT_CD'],
            "OVRS_EXCG_CD": "NASD", 
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": ""
        }
        holdings = {}
        cash = 0.0
        try:
            res = requests.get(url, headers=headers, params=params).json()
            if res['rt_cd'] == '0':
                for item in res['output1']:
                    qty = float(item['ovrs_cblc_qty'])
                    if qty > 0:
                        code = item['ovrs_pdno']
                        evlu_amt = float(item['ovrs_stck_evlu_amt'])
                        profit_rate = float(item['evlu_pfls_rt'])
                        avg_price = float(item['pchs_avg_pric'])
                        holdings[code] = {
                            "qty": int(qty),
                            "avg_price": avg_price,
                            "profit_rate": profit_rate,
                            "eval_amt": evlu_amt
                        }
                cash = self.get_buyable_cash()
            else:
                print_log(f"❌ 잔고 조회 실패: {res['msg1']}")
        except Exception as e:
            print_log(f"❌ 잔고 조회 에러: {e}")
            print_log(traceback.format_exc())
            
        return holdings, cash

    def get_open_orders(self, symbol, exch):
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
        tr_id = "TTTS3018R" if "REAL" in MODE else "VTTS3018R"
        headers = self.get_header(tr_id)
        params = {
            "CANO": self.cfg['CANO'], "ACNT_PRDT_CD": self.cfg['ACNT_PRDT_CD'],
            "OVRS_EXCG_CD": exch, "SORT_SQN": "DS", 
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
        }
        try:
            res = requests.get(url, headers=headers, params=params).json()
            if res['rt_cd'] == '0':
                return [ord for ord in res['output'] if ord['pdno'] == symbol]
        except: pass
        return []

    def cancel_all_orders(self, symbol, exch):
        orders = self.get_open_orders(symbol, exch)
        if not orders: 
            print_log(f"   {symbol} 취소할 미체결 내역 없음.")
            return

        print_log(f"🧹 {symbol} 미체결 주문 {len(orders)}건 취소 실행...")
        url_cancel = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        tr_id = "TTTT1004U" if "REAL" in MODE else "VTTT1004U" 
        headers_cancel = self.get_header(tr_id)
        for ord in orders:
            data = {
                "CANO": self.cfg['CANO'], "ACNT_PRDT_CD": self.cfg['ACNT_PRDT_CD'],
                "OVRS_EXCG_CD": exch, "PDNO": symbol, "ORGN_ODNO": ord['odno'],
                "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(ord['nccs_qty']), "OVRS_ORD_UNPR": "0", "ORD_SVR_DVSN_CD": "0"
            }
            requests.post(url_cancel, headers=headers_cancel, json=data)
            time.sleep(0.2)
        print_log(f"✅ {symbol} 취소 완료")

    def send_order(self, symbol, exch, qty, price, side, ord_type="00"):
        tr_id = "TTTT1002U" if side == "BUY" else "TTTT1006U"
        if "REAL" not in MODE: tr_id = "VTTT1002U" if side == "BUY" else "VTTT1006U"

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        headers = self.get_header(tr_id)
        data = {
            "CANO": self.cfg['CANO'], "ACNT_PRDT_CD": self.cfg['ACNT_PRDT_CD'],
            "OVRS_EXCG_CD": exch, "PDNO": symbol, "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price), "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": ord_type 
        }
        if price == 0: data["OVRS_ORD_UNPR"] = "0"
        try:
            res = requests.post(url, headers=headers, json=data).json()
            if res['rt_cd'] == '0':
                msg = f"{'🚀 매수' if side=='BUY' else '👋 매도'} 주문 전송: {symbol} {qty}주 @ ${price} ({ord_type})"
                print_log(f"✅ {msg}")
                send_discord(msg)
                return True
            else:
                print_log(f"❌ 주문 실패: {res['msg1']} ({res['msg_cd']})")
                return False
        except Exception as e:
            print_log(f"❌ 주문 에러: {e}")
            return False

# ==========================================
# [6. 기술적 지표]
# ==========================================
def calculate_indicators(hist):
    if hist is None or len(hist) < 120: return None
    df = hist.copy()
    
    sma20 = df['Close'].rolling(window=20).mean().iloc[-1]
    sma120 = df['Close'].rolling(window=120).mean().iloc[-1]
    std_dev = df['Close'].rolling(window=20).std().iloc[-1] 
    bb_lower = sma20 - (2 * std_dev)
    
    prev_sma20 = df['Close'].rolling(window=20).mean().iloc[-2]
    prev_close = df['Close'].iloc[-2]
    today_open = df['Open'].iloc[-1]

    df['up'] = df['High'] - df['High'].shift(1)
    df['down'] = df['Low'].shift(1) - df['Low']
    df['TR'] = pd.concat([df['High']-df['Low'], (df['High']-df['Close'].shift(1)).abs(), (df['Low']-df['Close'].shift(1)).abs()], axis=1).max(axis=1)
    
    df['+DM'] = 0.0; df.loc[(df['up'] > df['down']) & (df['up'] > 0), '+DM'] = df['up']
    df['-DM'] = 0.0; df.loc[(df['down'] > df['up']) & (df['down'] > 0), '-DM'] = df['down']
    
    n = 14; alpha = 1/n
    df['TR_s'] = df['TR'].ewm(alpha=alpha, adjust=False).mean()
    df['+DM_s'] = df['+DM'].ewm(alpha=alpha, adjust=False).mean()
    df['-DM_s'] = df['-DM'].ewm(alpha=alpha, adjust=False).mean()
    df['ADX'] = (abs(((df['+DM_s']/df['TR_s'])*100) - ((df['-DM_s']/df['TR_s'])*100)) / (((df['+DM_s']/df['TR_s'])*100) + ((df['-DM_s']/df['TR_s'])*100)) * 100).ewm(alpha=alpha, adjust=False).mean()

    return {
        "SMA20": sma20, "SMA120": sma120, "BB_LOW": bb_lower,
        "PREV_SMA20": prev_sma20, "PREV_CLOSE": prev_close,
        "TODAY_OPEN": today_open, "ADX": df['ADX'].iloc[-1], "BB_UP": sma20 + (2*std_dev)
    }

# ==========================================
# [7. Termux App (CLI)]
# ==========================================
class TermuxApp:
    def __init__(self, kis):
        self.kis = kis
        # 입력 스레드 시작
        input_t = threading.Thread(target=self.input_loop)
        input_t.daemon = True
        input_t.start()
        
        # [초기 실행] 0.5초 후 상태 출력
        time.sleep(0.5)
        self.process_command("현재")

    def input_loop(self):
        while True:
            try:
                cmd = input() 
                if cmd.strip():
                    self.process_command(cmd)
            except EOFError:
                break
            except Exception as e:
                print(f"입력 오류: {e}")

    def process_command(self, cmd):
        cmd = cmd.strip()
        print_log(f"\n[사용자 입력] >> {cmd}")
        
        if cmd == "현재":
            self.cmd_show_status()
        elif cmd == "검토":
            self.cmd_review()
        elif cmd == "취소":
            self.cmd_cancel_all()
        elif cmd.startswith("강제매도"):
            parts = cmd.split()
            if len(parts) == 2: self.cmd_manual_sell(parts[1])
        elif cmd.startswith("강제매수"):
            parts = cmd.split()
            if len(parts) == 2: self.cmd_manual_buy(parts[1])
        elif cmd.startswith("테스트매도"):
            parts = cmd.split()
            if len(parts) == 2: self.cmd_test_order(parts[1], "SELL")
        elif cmd.startswith("테스트매수"):
            parts = cmd.split()
            if len(parts) == 2: self.cmd_test_order(parts[1], "BUY")
        else: 
            print_log("❌ 알 수 없는 명령어입니다. (현재, 검토, 취소, 테스트매수/매도 [종목], 강제매수/매도 [종목])")

    def cmd_cancel_all(self):
        print_log("🧹 모든 미체결 주문 취소를 시도합니다...")
        for target in TARGETS:
            self.kis.cancel_all_orders(target['symbol'], target['exch'])
        print_log("✨ 취소 작업이 완료되었습니다.")

    def cmd_test_order(self, symbol, side):
        print_log(f"🧪 [{symbol}] {side} 테스트 주문 요청 (체결 안될 가격으로 1주)...")
        
        target = next((t for t in TARGETS if t['symbol'] == symbol), None)
        if not target:
            print_log(f"❌ 설정된 종목({symbol})이 아닙니다.")
            return

        curr = DataProvider.get_current_price(symbol)
        if not curr: 
            print_log(f"❌ {symbol} 현재가를 가져올 수 없어 테스트를 중단합니다.")
            return

        # 체결되지 않도록 가격 설정
        if side == "BUY":
            price = round(curr * 0.5, 2) # 현재가 -50%
            print_log(f"   가격 설정: ${curr} -> ${price} (매수)")
        else:
            price = round(curr * 1.5, 2) # 현재가 +50%
            print_log(f"   가격 설정: ${curr} -> ${price} (매도)")
            
            # 매도 테스트의 경우 잔고가 있어야 함 (없으면 거부됨)
            holdings, _ = self.kis.get_balance()
            if symbol not in holdings or holdings[symbol]['qty'] <= 0:
                print_log("⚠️ 주의: 해당 종목 잔고가 없어 매도 주문이 거부될 수 있습니다.")

        # 주문 전송 (지정가 '00')
        self.kis.send_order(symbol, target['exch'], 1, price, side, "00")

    def cmd_show_status(self):
        try:
            print_log("🔍 현재 상태 조회 중... (KIS API)")
            is_open, cur_time = get_market_status()
            if not is_open: print_log(f"🌑 현재 장 마감 상태입니다. (NY {cur_time})")

            holdings, cash = self.kis.get_balance()
            total_stock_val = 0.0

            # 보유 종목 리스트 생성 (없는 종목도 포함)
            stock_info_list = []
            for target in TARGETS:
                sym = target['symbol']
                qty = holdings.get(sym, {}).get('qty', 0)
                avg = holdings.get(sym, {}).get('avg_price', 0.0)
                
                # 현재가는 실시간 API 데이터가 없으면 yfinance로 조회
                cur_price = DataProvider.get_current_price(sym)
                if not cur_price and qty > 0: 
                    # API 잔고에 평가금액 역산 시도 or avg_price 사용 (fallback)
                    cur_price = avg 

                if cur_price is None: cur_price = 0.0

                val = qty * cur_price
                total_stock_val += val
                
                profit_amt = (cur_price - avg) * qty
                profit_rate = ((cur_price - avg) / avg * 100) if avg > 0 else 0.0

                stock_info_list.append({
                    "symbol": sym,
                    "qty": qty,
                    "cur_price": cur_price,
                    "avg_price": avg,
                    "val": val,
                    "profit_amt": profit_amt,
                    "profit_rate": profit_rate
                })

            total_equity = cash + total_stock_val
            
            print_log("══════════════════════════════════════════")
            for info in stock_info_list:
                weight = (info['val'] / total_equity * 100) if total_equity > 0 else 0
                print_log(f"🇺🇸 [{info['symbol']}] {info['qty']}주 | 현재가 ${info['cur_price']:.2f}")
                if info['qty'] > 0:
                    print_log(f"   평단 ${info['avg_price']:.2f} | 평가금 ${info['val']:.2f} ({weight:.1f}%)")
                    print_log(f"   수익 ${info['profit_amt']:.2f} ({info['profit_rate']:+.2f}%)")
                else:
                    print_log(f"   보유량 없음 (비중 0%)")
                print_log("-" * 30)

            print_log(f"💰 주문가능(통합): ${cash:,.2f}")
            print_log(f"💎 총 자본금: ${total_equity:,.2f}")
            print_log("══════════════════════════════════════════")

        except Exception as e:
            print_log(f"❌ 상태 조회 실패: {e}")
            print_log(traceback.format_exc())

    def cmd_review(self):
        print_log("🧐 현재 시장 상황을 검토합니다... (초보자 모드)")
        is_open, cur_time = get_market_status()
        if not is_open:
            print_log(f"🌑 현재는 장 마감 상태입니다. (NY {cur_time})")
            print_log("   가장 최근 데이터를 기준으로 분석해드릴게요!\n")

        for target in TARGETS:
            sym = target['symbol']
            print_log(f"📌 [{sym}] 분석 결과")
            
            hist = DataProvider.get_daily_history(sym)
            if hist is None:
                print_log("   ⚠️ 데이터를 불러오지 못했어요. 잠시 후 다시 시도해주세요.")
                continue

            inds = calculate_indicators(hist)
            if not inds:
                print_log("   ⚠️ 지표 계산에 필요한 데이터가 부족해요.")
                continue
            
            curr = DataProvider.get_current_price(sym)
            if not curr: curr = hist['Close'].iloc[-1]

            # 조건 분석
            # 1. 120일선 (장기 추세)
            cond_trend = curr > inds['SMA120']
            mark_trend = "[O]" if cond_trend else "[X]"
            trend_msg = "상승 추세예요 (정배열) 👍" if cond_trend else "하락 추세예요 (역배열) 👎"
            print_log(f"   1. {mark_trend} 장기 추세 (120일선): ${inds['SMA120']:.2f} vs 현재 ${curr:.2f} -> {trend_msg}")

            # 2. 20일선 및 모멘텀 (진입 시점)
            cond_cross = (inds['PREV_CLOSE'] < inds['PREV_SMA20']) and (curr > inds['SMA20'])
            
            today_low = hist['Low'].iloc[-1]
            touched_low = today_low < inds['BB_LOW']
            reclaimed = curr > inds['BB_LOW']
            cond_reclaim = touched_low and reclaimed
            
            # 진입 조건 충족 여부 마킹
            is_entry_signal = cond_cross or cond_reclaim
            mark_entry = "[O]" if is_entry_signal else "[X]"

            if cond_cross:
                entry_msg = "골든크로스 발생! (20일선 돌파) ✨"
            elif cond_reclaim:
                entry_msg = "반등 신호 발생! (볼린저밴드 하단 회복) ✨"
            else:
                entry_msg = "아직 진입 신호가 없어요. (20일선 아래거나 횡보 중) zzz"
            
            print_log(f"   2. {mark_entry} 진입 타이밍: {entry_msg}")

            # 3. ADX (추세 강도)
            cond_adx = inds['ADX'] >= 25
            mark_adx = "[O]" if cond_adx else "[X]"
            adx_msg = f"추세가 강해요 (ADX {inds['ADX']:.1f}) 🔥" if cond_adx else f"추세가 약해요 (ADX {inds['ADX']:.1f}) ☁️"
            print_log(f"   3. {mark_adx} 추세 강도: {adx_msg}")

            # 매도 조건 체크
            if curr < inds['SMA20']:
                print_log("   🚨 [주의] 현재가가 20일선 아래입니다. 보유 중이라면 매도를 고려해야 해요.")

            # 종합 결론
            if cond_trend and is_entry_signal and cond_adx:
                print_log("   🎉 결론: 모든 조건 만족! 매수할 만한 타이밍입니다!")
            else:
                print_log("   ✋ 결론: 아직은 지켜볼 때입니다. 조건이 모두 맞을 때까지 기다리세요.")
            print_log("-" * 30)

    def cmd_manual_sell(self, symbol):
        print_log(f"⚠️ [{symbol}] 강제 매도 요청...")
        holdings, _ = self.kis.get_balance()
        if symbol not in holdings: return print_log("❌ 미보유 종목")
        curr = DataProvider.get_current_price(symbol)
        if not curr: return
        
        target = next((t for t in TARGETS if t['symbol'] == symbol), None)
        if target:
            self.kis.cancel_all_orders(symbol, target['exch'])
            if self.kis.send_order(symbol, target['exch'], holdings[symbol]['qty'], round(curr * 0.95, 2), "SELL", "00"):
                status_mgr.set_ignore_sync(symbol, 3600)

    def cmd_manual_buy(self, symbol):
        print_log(f"⚠️ [{symbol}] 강제 매수 요청 (1주)...")
        curr = DataProvider.get_current_price(symbol)
        if not curr: return

        target = next((t for t in TARGETS if t['symbol'] == symbol), None)
        if target:
            self.kis.send_order(symbol, target['exch'], 1, round(curr * 1.05, 2), "BUY", "00")

# ==========================================
# [8. 전략 스레드]
# ==========================================
def strategy_thread(kis):
    ny_tz = pytz.timezone('America/New_York')
    print_log("🤖 미국치킨 V1.0 (Termux) 가동")
    
    prev_holdings_snapshot = {}
    last_wait_log = 0 

    while True:
        try:
            now_ny = datetime.now(ny_tz)
            current_time = now_ny.strftime("%H:%M")
            
            # 주말 체크 (0:월 ~ 6:일)
            if now_ny.weekday() >= 5:
                if time.time() - last_wait_log > 3600:
                    print_log(f"⏳ 주말 휴장 중... (NY {current_time})")
                    last_wait_log = time.time()
                time.sleep(60)
                continue

            # 장 시작 전 / 장 마감 후 로직
            if current_time < "09:30":
                if time.time() - last_wait_log > 1800:
                    print_log(f"⏳ 장 시작 대기 중... (현재 NY: {current_time})")
                    last_wait_log = time.time()
                time.sleep(60)
                continue
            
            if current_time >= "16:00":
                if not status_mgr.data.get("daily_reset_done"):
                    status_mgr.reset_daily()
                    status_mgr.data["daily_reset_done"] = True
                    print_log("🌙 장 마감. 금일 데이터 리셋 완료.")
                if current_time == "16:05": print_log("👋 [안내] 16:05 경과. 봇 종료 가능.")
                time.sleep(60) 
                continue
            
            if status_mgr.data.get("daily_reset_done"): status_mgr.data["daily_reset_done"] = False

            # 루프 1회차 동기화
            holdings, cash = kis.get_balance()
            
            # 외부 거래 감지
            for sym in TARGETS:
                symbol = sym['symbol']
                current_qty = holdings.get(symbol, {}).get('qty', 0)
                prev_qty = prev_holdings_snapshot.get(symbol, 0)
                if current_qty > prev_qty: status_mgr.reset_max_profit(symbol)
                prev_holdings_snapshot[symbol] = current_qty

            # [Phase A] 09:30 ~ 09:40 (시초가 갭상승 익절)
            if "09:30" <= current_time < "09:40":
                if not status_mgr.data['phase_a_done']:
                    for target in TARGETS:
                        sym = target['symbol']
                        if sym in holdings:
                            kis.cancel_all_orders(sym, target['exch'])
                            # 캐시된 데이터를 활용하여 효율적 조회
                            hist = DataProvider.get_daily_history(sym, days=30)
                            if hist is not None:
                                inds = calculate_indicators(hist)
                                if inds:
                                    sell_qty = int(holdings[sym]['qty'] * 0.5)
                                    if sell_qty > 0:
                                        print_log(f"[Phase A] {sym} 50% 익절 주문 (${inds['BB_UP']:.2f})")
                                        kis.send_order(sym, target['exch'], sell_qty, round(inds['BB_UP'], 2), "SELL", "00")
                    status_mgr.set_phase_a_done(True)

            # [Phase B] 09:30 ~ 15:50 (Trailing Stop & Stop Loss)
            if "09:30" <= current_time < "15:50":
                for target in TARGETS:
                    sym = target['symbol']
                    if status_mgr.is_sync_ignored(sym): continue
                    
                    if sym in holdings:
                        info = holdings[sym]
                        curr = DataProvider.get_current_price(sym)
                        if not curr: continue
                        
                        rate = (curr - info['avg_price']) / info['avg_price'] * 100
                        status_mgr.update_max_profit(sym, rate)
                        max_rate = status_mgr.get_max_profit(sym)
                        market_sell = round(curr * 0.95, 2)

                        if rate <= -5.0:
                            print_log(f"🚨 [손절] {sym} -5% 도달")
                            kis.cancel_all_orders(sym, target['exch'])
                            if kis.send_order(sym, target['exch'], info['qty'], market_sell, "SELL", "00"):
                                status_mgr.set_ignore_sync(sym, 3600)
                        elif max_rate >= 10.0 and (max_rate - rate) >= 3.0:
                            print_log(f"📉 [익절] {sym} 고점 대비 하락")
                            kis.cancel_all_orders(sym, target['exch'])
                            if kis.send_order(sym, target['exch'], info['qty'], market_sell, "SELL", "00"):
                                status_mgr.set_ignore_sync(sym, 3600)

            # [Phase C] 15:50 ~ 16:00 (진입 판단)
            if "15:50" <= current_time < "16:00":
                print_log("⚖️ [Phase C] 장 마감 진입 판단")
                
                # Equity 계산
                curr_vals = 0.0
                curr_prices = {}
                for t in TARGETS:
                    p = DataProvider.get_current_price(t['symbol'])
                    if p: curr_prices[t['symbol']] = p
                    if t['symbol'] in holdings and p:
                        curr_vals += holdings[t['symbol']]['qty'] * p
                
                # 통합증거금 포함 총 자본
                total_equity = cash + curr_vals
                target_alloc = total_equity * 0.5 
                print_log(f"💰 Equity: ${total_equity:,.2f} / Target: ${target_alloc:,.2f}")

                buy_list = []

                for target in TARGETS:
                    sym = target['symbol']
                    if status_mgr.is_sync_ignored(sym): continue
                    
                    # 1. 미체결 확인
                    if kis.get_open_orders(sym, target['exch']):
                        print_log(f"⏳ [중복방지] {sym} 미체결 존재. 진입 보류.")
                        continue

                    # 2. 데이터 조회 (캐싱 적용됨)
                    curr = curr_prices.get(sym)
                    hist = DataProvider.get_daily_history(sym)
                    if hist is None or not curr: continue
                    inds = calculate_indicators(hist)
                    if not inds: continue
                    
                    # 3. 매도 로직 (SMA 20 이탈 시 전량 매도)
                    real_qty = holdings.get(sym, {}).get('qty', 0)
                    if real_qty > 0 and curr < inds['SMA20']:
                        print_log(f"📉 [추세이탈] {sym} 20일선 붕괴 -> 매도")
                        market_sell = round(curr * 0.95, 2)
                        if kis.send_order(sym, target['exch'], real_qty, market_sell, "SELL", "00"):
                            status_mgr.set_ignore_sync(sym, 3600)
                        continue
                    
                    # 4. 매수 로직 (ADX + SMA + Reclaim)
                    cond_trend = curr > inds['SMA120'] # 장기 정배열
                    
                    # A전략: 골든크로스
                    cond_cross = (inds['PREV_CLOSE'] < inds['PREV_SMA20']) and (curr > inds['SMA20'])
                    
                    # B전략: 볼린저 밴드 하단 Reclaim (찌르고 회복)
                    today_low = hist['Low'].iloc[-1]
                    today_open = hist['Open'].iloc[-1]
                    touched_low = today_low < inds['BB_LOW']
                    reclaimed = curr > inds['BB_LOW']        
                    is_green = curr > today_open             
                    cond_reclaim = touched_low and reclaimed and is_green

                    cond_adx = inds['ADX'] >= 25 # 강한 추세
                    
                    if cond_trend and (cond_cross or cond_reclaim):
                        if cond_adx:
                            # 가상 잔고 포함하여 필요 금액 계산
                            virtual_qty = status_mgr.get_virtual_qty(sym, real_qty)
                            held_amt = virtual_qty * curr
                            needed_amt = target_alloc - held_amt
                            
                            if needed_amt > 10:
                                log_msg = "골든크로스" if cond_cross else "밴드회복"
                                print_log(f"📈 [매수신호] {sym} ({log_msg}, ADX:{inds['ADX']:.1f})")
                                buy_list.append({
                                    "target": target,
                                    "amount": needed_amt,
                                    "price": curr,
                                    "qty": real_qty
                                })
                        else:
                            print_log(f"⚠️ [매수패스] {sym} 추세 약함 (ADX: {inds['ADX']:.1f} < 25)")

                # [Phase D] TWAP 매수
                if buy_list:
                    for i in range(3):
                        now_str = datetime.now(ny_tz).strftime("%H:%M:%S")
                        is_last = (now_str >= "15:59:00")
                        rem_mult = 3 - i
                        
                        print_log(f"💸 TWAP 매수 ({i+1}/3)")
                        
                        for order in buy_list:
                            sym = order['target']['symbol']
                            exch = order['target']['exch']
                            curr = DataProvider.get_current_price(sym) or order['price']
                            
                            chunk = order['amount'] / 3.0
                            if is_last: chunk *= rem_mult
                            
                            qty = int(chunk / curr)
                            if qty > 0:
                                if kis.send_order(sym, exch, qty, round(curr * 1.05, 2), "BUY", "00"):
                                    status_mgr.record_pending_buy(sym, qty, order['qty'])
                        
                        if is_last: break
                        if i < 2: time.sleep(150)
                
                time.sleep(600)

            time.sleep(60)

        except Exception as e:
            print_log(f"에러 발생: {traceback.format_exc()}")
            time.sleep(60)

if __name__ == "__main__":
    kis = KisUS()
    # GUI 제거: TermuxApp이 CLI 역할
    app = TermuxApp(kis)
    t = threading.Thread(target=strategy_thread, args=(kis,))
    t.daemon = True
    t.start()
    # 메인 스레드 유지
    while True:
        time.sleep(1)