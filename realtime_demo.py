#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phần 0: Config & Khởi tạo
"""

import os
import json
import logging
from datetime import datetime

import requests
from FiinQuantX import FiinSession

# ==============================
# Helpers
# ==============================

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def load_config(path="config.json"):
    """Đọc config từ file JSON"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Không tìm thấy {path}, vui lòng tạo config.json trước")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ==============================
# Telegram Reporter
# ==============================
class TelegramReporter:
    def __init__(self, token, chat_id, message_thread_id=None):
        self.token = token
        self.chat_id = chat_id
        self.thread_id = message_thread_id
        self.base = f"https://api.telegram.org/bot{token}"

    def send(self, text: str):
        """Gửi message đơn giản sang Telegram"""
        try:
            url = f"{self.base}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
            }
            if self.thread_id is not None:
                payload["message_thread_id"] = self.thread_id
            r = requests.post(url, data=payload, timeout=5)
            if r.status_code != 200:
                logging.warning(f"Telegram send failed: {r.text}")
        except Exception as e:
            logging.warning(f"Telegram error: {e}")

# ==============================
# INIT
# ==============================
def init_all(config_path="config.json"):
    # Đọc config
    cfg = load_config(config_path)

    # Setup logger
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M"
    )

    # Login FiinQuant
    logging.info("Đăng nhập FiinQuant...")
    username = cfg["fiinquant"]["username"]
    password = cfg["fiinquant"]["password"]

    client = FiinSession(username=username, password=password).login()
    if client is None:
        raise RuntimeError("Login FiinQuant thất bại")

    logging.info("Login thành công!")

    # Init Telegram
    tele_cfg = cfg.get("telegram", {})
    reporter = TelegramReporter(
        token=tele_cfg.get("bot_token"),
        chat_id=tele_cfg.get("chat_id"),
        message_thread_id=tele_cfg.get("message_thread_id", None)
    )

    reporter.send(f"✅ [{now_str()}] Khởi tạo pipeline thành công!")

    # Tạo thư mục paths nếu chưa có
    for k, p in cfg.get("paths", {}).items():
        os.makedirs(p, exist_ok=True)

    return cfg, client, reporter

# ==============================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phần 1: Data Layer (Historical + Realtime)
"""

import os
import pandas as pd
import logging
from collections import deque

# Giả sử đã có hàm init_all() từ Phần 0
# from realtime_trading import init_all

# ==============================
# Data Layer
# ==============================
class DataLayer:
    def __init__(self, client, cfg, reporter=None):
        self.client = client
        self.cfg = cfg
        self.reporter = reporter

        # danh sách tickers VN30
        self.tickers = list(client.TickerList(ticker="VN30"))
        logging.info(f"Universe VN30 gồm {len(self.tickers)} mã: {self.tickers}")

        # DataFrame lưu dữ liệu từng ticker
        self.data_store = {t: pd.DataFrame() for t in self.tickers}

        # Hàng đợi realtime (chứa dict row)
        self.realtime_queue = deque(maxlen=10000)

    def load_historical(self, path_csv="vnindex_price_fa_merged.csv"):
        """Load dữ liệu lịch sử từ file đã merge sẵn (vnindex_price_fa_merged)"""
        if not os.path.exists(path_csv):
            raise FileNotFoundError(f"Không tìm thấy file {path_csv}")

        df_hist = pd.read_csv(path_csv)
        df_hist["timestamp"] = pd.to_datetime(df_hist["timestamp"])
        logging.info(f"Đã load dữ liệu lịch sử từ {path_csv}, shape={df_hist.shape}")

        # chỉ lấy VN30
        df_hist = df_hist[df_hist["ticker"].isin(self.tickers)]

        # gán vào data_store
        for t in self.tickers:
            dft = df_hist[df_hist["ticker"] == t].sort_values("timestamp").copy()
            if not dft.empty:
                self.data_store[t] = dft
        logging.info("Đã load dữ liệu lịch sử cho VN30")

    def start_realtime(self):
        """Đăng ký realtime streaming VN30"""
        def on_update(data):
            try:
                df_new = data.to_dataFrame()
                for _, row in df_new.iterrows():
                    t = row["ticker"]
                    if t in self.data_store:
                        # nối thêm row mới
                        self.data_store[t] = pd.concat(
                            [self.data_store[t], pd.DataFrame([row])],
                            ignore_index=True
                        ).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
                        # đẩy vào queue
                        self.realtime_queue.append(row.to_dict())
                logging.info(f"Realtime update: {len(df_new)} rows")
            except Exception as e:
                logging.warning(f"Realtime callback error: {e}")

        self.client.Fetch_Trading_Data(
            realtime=True,
            tickers=self.tickers,
            fields=["open","high","low","close","volume"],
            adjusted=True,
            by=self.cfg["pipeline"].get("bar_interval", "1m"),
            period=1,
            callback=on_update
        )
        logging.info("Realtime streaming VN30 đã bật thành công")

    def get_latest_data(self, ticker, n=50):
        """Trả về N dòng cuối cùng của ticker"""
        if ticker not in self.data_store:
            return pd.DataFrame()
        return self.data_store[ticker].tail(n).copy()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phần 2: Feature Engineering
"""

import numpy as np
import pandas as pd
import logging

class FeatureEngineer:
    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg
        self.fi = client.FiinIndicator()
        self.lookback = cfg["pipeline"]["state_lookback"]

    def add_ta_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Thêm các chỉ báo kỹ thuật vào DataFrame"""
        s = df.copy().reset_index(drop=True).sort_values("timestamp")

        # EMA, SMA, RSI, MACD, ATR
        s["ema_5"] = self.fi.ema(s["close"], window=5)
        s["ema_20"] = self.fi.ema(s["close"], window=20)
        s["sma_20"] = self.fi.sma(s["close"], window=20)
        s["rsi_14"] = self.fi.rsi(s["close"], window=14)
        s["macd"] = self.fi.macd(s["close"])
        s["atr_14"] = self.fi.atr(s["high"], s["low"], s["close"], window=14)

        # Tính thêm return, vol_zscore
        s["ret1"] = s["close"].pct_change().fillna(0)
        s["vol_z"] = (s["volume"] - s["volume"].rolling(20).mean()).fillna(0)

        # Fill missing
        s = s.fillna(method="ffill").fillna(method="bfill").fillna(0)
        return s

    def make_state(self, df: pd.DataFrame) -> np.ndarray:
        """
        Chuyển dữ liệu ticker thành vector state cho agent RL
        - Input: df OHLCV + indicators
        - Output: numpy vector (lookback * n_features,)
        """
        if df is None or len(df) < self.lookback:
            return None

        df_ta = self.add_ta_indicators(df)

        features = [
            "close", "volume",
            "ema_5", "ema_20", "sma_20",
            "rsi_14", "macd", "atr_14",
            "ret1", "vol_z"
        ]
        if not all(f in df_ta.columns for f in features):
            logging.warning("Thiếu cột trong dữ liệu, trả về None")
            return None

        window = df_ta.tail(self.lookback)
        X = window[features].values.astype(np.float32)

        # Chuẩn hoá mỗi feature theo z-score (theo window)
        X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-6)

        # Flatten thành vector 1D
        return X.reshape(-1)
# Phần 3: RL Models (A3C & DDPG)
# Yêu cầu: torch, numpy
# Thiết kế:
# - A3CAgent: Actor-Critic (single-process A2C-like) với pretrain_supervised + optional policy gradient fine-tune
# - DDPGAgent: Actor + Critic, replay buffer, pretrain_supervised (regression) + train_from_buffer()

import os
import time
import json
import logging
from collections import deque, namedtuple
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset

# --------------------
# Utils
# --------------------
def now_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")

# --------------------
# Simple MLPs
# --------------------
class MLP(nn.Module):
    def __init__(self, input_dim, hidden_dims=(128,64), activation=nn.ReLU):
        super().__init__()
        layers = []
        prev = input_dim
        for h in hidden_dims:
            layers.append(nn.Linear(prev, h))
            layers.append(activation())
            prev = h
        self.net = nn.Sequential(*layers)
    def forward(self, x):
        return self.net(x)

class ActorDiscrete(nn.Module):
    """Actor for discrete actions (sell, hold, buy)"""
    def __init__(self, input_dim, hidden=(128,64), n_actions=3):
        super().__init__()
        self.backbone = MLP(input_dim, hidden)
        self.logits = nn.Linear(hidden[-1], n_actions)
    def forward(self, x):
        h = self.backbone(x)
        return self.logits(h)

class Critic(nn.Module):
    """State-value critic"""
    def __init__(self, input_dim, hidden=(128,64)):
        super().__init__()
        self.backbone = MLP(input_dim, hidden)
        self.value = nn.Linear(hidden[-1], 1)
    def forward(self, x):
        h = self.backbone(x)
        return self.value(h).squeeze(-1)

class ActorContinuous(nn.Module):
    """Actor outputting continuous action in [-1,1]"""
    def __init__(self, input_dim, hidden=(128,64)):
        super().__init__()
        self.backbone = MLP(input_dim, hidden)
        self.mu = nn.Linear(hidden[-1], 1)
        self.log_std = nn.Parameter(torch.zeros(1))  # trainable std
    def forward(self, x):
        h = self.backbone(x)
        mu = torch.tanh(self.mu(h))  # keep in [-1,1]
        std = torch.exp(self.log_std)
        return mu, std

class CriticQ(nn.Module):
    """Q(s,a) critic for DDPG (takes state||action)"""
    def __init__(self, state_dim, action_dim=1, hidden=(128,64)):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim + action_dim, hidden[0]),
            nn.ReLU(),
            nn.Linear(hidden[0], hidden[1]),
            nn.ReLU(),
            nn.Linear(hidden[1], 1)
        )
    def forward(self, s, a):
        x = torch.cat([s, a], dim=-1)
        return self.net(x).squeeze(-1)

# --------------------
# A3C / A2C-like Agent (single process A2C)
# --------------------
class A3CAgent:
    def __init__(self, input_dim, ckpt_path, device=None, lr=2e-4):
        self.input_dim = input_dim
        self.device = device or (torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu"))
        self.actor = ActorDiscrete(input_dim).to(self.device)
        self.critic = Critic(input_dim).to(self.device)
        self.opt = optim.Adam(list(self.actor.parameters()) + list(self.critic.parameters()), lr=lr)
        self.ckpt_path = ckpt_path

    def predict(self, state_vec):
        """state_vec: numpy 1D -> returns action in {-1,0,1}"""
        self.actor.eval()
        with torch.no_grad():
            x = torch.from_numpy(state_vec).float().to(self.device).unsqueeze(0)
            logits = self.actor(x)
            probs = torch.softmax(logits, dim=-1).cpu().numpy().ravel()
            a = int(np.argmax(probs))  # 0,1,2
        return a - 1  # map to -1,0,1

    def save(self):
        payload = {
            "actor": self.actor.state_dict(),
            "critic": self.critic.state_dict()
        }
        torch.save(payload, self.ckpt_path)
        logging.info(f"[{now_str()}] A3C saved -> {self.ckpt_path}")

    def load(self):
        if os.path.exists(self.ckpt_path):
            payload = torch.load(self.ckpt_path, map_location=self.device)
            self.actor.load_state_dict(payload["actor"])
            self.critic.load_state_dict(payload["critic"])
            logging.info(f"[{now_str()}] A3C loaded from {self.ckpt_path}")
            return True
        return False

    # ---- Supervised pretrain: classify next-return sign (-1,0,1) ----
    def pretrain_supervised(self, X, y, epochs=10, batch_size=128):
        """
        X: numpy (N, input_dim)
        y: numpy (N,) values in {-1,0,1}
        """
        self.actor.train(); self.critic.train()
        y_idx = (y + 1).astype(np.int64)  # -1->0, 0->1, 1->2
        ds = TensorDataset(torch.from_numpy(X).float(), torch.from_numpy(y_idx))
        loader = DataLoader(ds, batch_size=batch_size, shuffle=True)
        ce = nn.CrossEntropyLoss()
        for ep in range(epochs):
            total_loss = 0.0
            for xb, yb in loader:
                xb = xb.to(self.device); yb = yb.to(self.device)
                logits = self.actor(xb)
                loss = ce(logits, yb)
                self.opt.zero_grad(); loss.backward(); self.opt.step()
                total_loss += loss.item()
            logging.info(f"[{now_str()}] A3C pretrain epoch {ep+1}/{epochs}, loss={total_loss:.4f}")
        self.save()

    # ---- Simple policy-gradient fine-tune on historical episodes (advantage actor-critic one-step) ----
    def fine_tune_rl(self, dataset_states, dataset_rewards, epochs=5, gamma=0.99, batch_size=256):
        """
        dataset_states: numpy (N, input_dim)
        dataset_rewards: numpy (N,) immediate reward (e.g. next return)
        This routine treats samples independently: compute advantage = reward - V(s), and update actor, critic.
        Not a full episodic A3C, but a light fine-tune using advantage.
        """
        self.actor.train(); self.critic.train()
        ds = TensorDataset(torch.from_numpy(dataset_states).float(), torch.from_numpy(dataset_rewards).float())
        loader = DataLoader(ds, batch_size=batch_size, shuffle=True)
        for ep in range(epochs):
            tot_loss = 0.0
            for xb, rb in loader:
                xb = xb.to(self.device); rb = rb.to(self.device)
                logits = self.actor(xb)
                probs = torch.softmax(logits, dim=-1)
                logp = torch.log_softmax(logits, dim=-1)
                values = self.critic(xb).squeeze(-1)
                # construct pseudo-actions from reward sign (teacher forcing)
                actions = torch.clamp(torch.sign(rb), -1, 1).long() + 1  # -1->0,0->1,1->2
                # Advantage
                adv = (rb - values).detach()
                # policy loss (negative log prob * adv)
                logp_actions = logp[range(len(actions)), actions]
                policy_loss = -(logp_actions * adv).mean()
                value_loss = nn.MSELoss()(values, rb)
                loss = policy_loss + 0.5 * value_loss
                self.opt.zero_grad(); loss.backward(); self.opt.step()
                tot_loss += loss.item()
            logging.info(f"[{now_str()}] A3C fine-tune ep {ep+1}/{epochs}, loss={tot_loss:.4f}")
        self.save()

# --------------------
# DDPG Agent (simplified)
# --------------------
Transition = namedtuple("Transition", ("s", "a", "r", "s2", "done"))

class ReplayBuffer:
    def __init__(self, capacity=200000):
        self.buffer = deque(maxlen=capacity)
    def push(self, *args):
        self.buffer.append(Transition(*args))
    def sample(self, batch_size):
        idx = np.random.choice(len(self.buffer), batch_size, replace=False)
        batch = [self.buffer[i] for i in idx]
        return Transition(*zip(*batch))
    def __len__(self):
        return len(self.buffer)

class DDPGAgent:
    def __init__(self, input_dim, ckpt_path, device=None, gamma=0.99, tau=1e-3, lr=1e-3):
        self.input_dim = input_dim
        self.device = device or (torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu"))
        self.actor = ActorContinuous(input_dim).to(self.device)
        self.actor_target = ActorContinuous(input_dim).to(self.device)
        self.critic = CriticQ(input_dim).to(self.device)
        self.critic_target = CriticQ(input_dim).to(self.device)
        # copy weights
        self.actor_target.load_state_dict(self.actor.state_dict())
        self.critic_target.load_state_dict(self.critic.state_dict())
        self.a_opt = optim.Adam(self.actor.parameters(), lr=lr)
        self.c_opt = optim.Adam(self.critic.parameters(), lr=lr)
        self.gamma = gamma; self.tau = tau
        self.replay = ReplayBuffer(capacity=200000)
        self.ckpt_path = ckpt_path

    def predict(self, state_vec):
        """Return discrete action based on continuous actor output thresholded"""
        self.actor.eval()
        with torch.no_grad():
            x = torch.from_numpy(state_vec).float().to(self.device).unsqueeze(0)
            mu, std = self.actor(x)
            val = mu.cpu().numpy().ravel()[0]
        # threshold to discrete signal
        if val > 0.2:
            return 1
        elif val < -0.2:
            return -1
        else:
            return 0

    def save(self):
        payload = {
            "actor": self.actor.state_dict(),
            "critic": self.critic.state_dict()
        }
        torch.save(payload, self.ckpt_path)
        logging.info(f"[{now_str()}] DDPG saved -> {self.ckpt_path}")

    def load(self):
        if os.path.exists(self.ckpt_path):
            payload = torch.load(self.ckpt_path, map_location=self.device)
            self.actor.load_state_dict(payload["actor"])
            self.critic.load_state_dict(payload["critic"])
            # sync target nets
            self.actor_target.load_state_dict(self.actor.state_dict())
            self.critic_target.load_state_dict(self.critic.state_dict())
            logging.info(f"[{now_str()}] DDPG loaded from {self.ckpt_path}")
            return True
        return False

    # ---- Supervised pretrain for actor (regress to target continuous action) ----
    def pretrain_supervised(self, X, y_cont, epochs=10, batch_size=128, lr=1e-3):
        """
        X: (N,input_dim), y_cont: (N,) continuous targets in [-1,1] (e.g. next return clipped)
        """
        self.actor.train()
        ds = TensorDataset(torch.from_numpy(X).float(), torch.from_numpy(y_cont).float().unsqueeze(1))
        loader = DataLoader(ds, batch_size=batch_size, shuffle=True)
        opt = optim.Adam(self.actor.parameters(), lr=lr)
        loss_fn = nn.MSELoss()
        for ep in range(epochs):
            tot = 0.0
            for xb, yb in loader:
                xb = xb.to(self.device); yb = yb.to(self.device)
                pred_mu, _ = self.actor(xb)
                loss = loss_fn(pred_mu, yb)
                opt.zero_grad(); loss.backward(); opt.step()
                tot += loss.item()
            logging.info(f"[{now_str()}] DDPG pretrain ep {ep+1}/{epochs}, loss={tot:.4f}")
        self.save()

    # ---- Add transitions collected from historical dataset ----
    def add_transitions_from_history(self, states, actions, rewards, next_states, dones):
        """
        states: list/np (N, input_dim)
        actions: list/np (N,) continuous in [-1,1] or discrete mapped to continuous
        rewards: list/np (N,)
        next_states: list/np (N,input_dim)
        dones: list/np (N,) bool
        """
        for s,a,r,s2,d in zip(states, actions, rewards, next_states, dones):
            s_t = np.asarray(s, dtype=np.float32)
            s2_t = np.asarray(s2, dtype=np.float32)
            a_t = np.array([float(a)], dtype=np.float32)
            self.replay.push(s_t, a_t, float(r), s2_t, bool(d))

    # ---- Train one step from replay buffer ----
    def train_from_buffer(self, batch_size=256, updates=1):
        if len(self.replay) < batch_size:
            return
        for _ in range(updates):
            batch = self.replay.sample(batch_size)
            s = torch.from_numpy(np.stack(batch.s)).float().to(self.device)
            a = torch.from_numpy(np.stack(batch.a)).float().to(self.device)
            r = torch.from_numpy(np.array(batch.r)).float().to(self.device)
            s2 = torch.from_numpy(np.stack(batch.s2)).float().to(self.device)
            done = torch.from_numpy(np.array(batch.done).astype(np.float32)).float().to(self.device)

            # critic update
            with torch.no_grad():
                a2_mu, _ = self.actor_target(s2)
                q_next = self.critic_target(s2, a2_mu)
                q_target = r + (1.0 - done) * self.gamma * q_next
            q_pred = self.critic(s, a)
            c_loss = nn.MSELoss()(q_pred, q_target)
            self.c_opt.zero_grad(); c_loss.backward(); self.c_opt.step()

            # actor update (maximize Q)
            a_mu, _ = self.actor(s)
            q_val = self.critic(s, a_mu)
            a_loss = -q_val.mean()
            self.a_opt.zero_grad(); a_loss.backward(); self.a_opt.step()

            # soft update targets
            for tp, p in zip(self.actor_target.parameters(), self.actor.parameters()):
                tp.data.copy_(tp.data * (1.0 - self.tau) + p.data * self.tau)
            for tp, p in zip(self.critic_target.parameters(), self.critic.parameters()):
                tp.data.copy_(tp.data * (1.0 - self.tau) + p.data * self.tau)

    # ---- Simple wrapper to run multi-epoch training from buffer ----
    def fit_from_buffer(self, epochs=1, batch_size=256, updates_per_epoch=10):
        for ep in range(epochs):
            self.train_from_buffer(batch_size=batch_size, updates=updates_per_epoch)
            logging.info(f"[{now_str()}] DDPG fit epoch {ep+1}/{epochs}")

def init_agents(cfg, data_store):
    import os, logging
    model_dir = cfg["paths"]["model_dir"]
    os.makedirs(model_dir, exist_ok=True)

    ckpt_a3c = os.path.join(model_dir, "a3c.pt")
    ckpt_ddpg = os.path.join(model_dir, "ddpg.pt")

    lookback = cfg["pipeline"]["state_lookback"]
    n_features = 10
    input_dim = lookback * n_features

    # Init agents
    a3c_agent = A3CAgent(input_dim, ckpt_path=ckpt_a3c)
    ddpg_agent = DDPGAgent(input_dim, ckpt_path=ckpt_ddpg)

    # 🚨 Nếu chưa có checkpoint thì chỉ log cảnh báo, không gọi train()
    if not os.path.exists(ckpt_a3c):
        logging.warning("⚠️ Chưa có checkpoint A3C — agent sẽ dự đoán random/không chính xác.")
    if not os.path.exists(ckpt_ddpg):
        logging.warning("⚠️ Chưa có checkpoint DDPG — agent sẽ dự đoán random/không chính xác.")

    return a3c_agent, ddpg_agent


# --------------------
# Example usage (how to call from main pipeline)
# --------------------
# - Build dataset in main:
#     X (N,input_dim): from FeatureEngineer over sliding windows
#     y_sign (N,) : np.sign(next_return) in {-1,0,1}
#     y_cont (N,) : clipped next_return into [-1,1]
#
# - Instantiate agents:
#     a3c = A3CAgent(input_dim, ckpt_path="models/a3c.pth")
#     ddpg = DDPGAgent(input_dim, ckpt_path="models/ddpg.pth")
#
# - Try load:
#     a3c.load(); ddpg.load()
#
# - If not loaded, pretrain supervised:
#     a3c.pretrain_supervised(X, y_sign, epochs=5)
#     ddpg.pretrain_supervised(X, y_cont, epochs=5)
#
# - For online inference: action_a3c = a3c.predict(state_vec); action_ddpg = ddpg.predict(state_vec)
#
# - Optional: populate ddpg.replay with transitions from historical sliding windows and call ddpg.fit_from_buffer()

# End of Part 3


# Part 4: Portfolio & Risk Management
# Requires: pandas, logging
# Timestamp format used throughout: "%Y-%m-%d %H:%M"

import time
import math
import logging
from datetime import datetime
import pandas as pd

TS_FMT = "%Y-%m-%d %H:%M"

def now_str():
    return datetime.now().strftime(TS_FMT)

class Position:
    """
    Position object storing basic info per ticker.
    - size (number of shares)
    - avg_price (entry price)
    - direction (1 long; -1 short not implemented)
    - sl, tp (absolute price levels)
    - entry_time (timestamp string)
    - holding_days (int)
    """
    def __init__(self, ticker, size, avg_price, direction=1, sl=None, tp=None, entry_time=None):
        self.ticker = ticker
        self.size = float(size)
        self.avg_price = float(avg_price)
        self.direction = int(direction)
        self.sl = sl
        self.tp = tp
        self.entry_time = entry_time or now_str()
        self.holding_days = 0

    def to_dict(self):
        return {
            "ticker": self.ticker,
            "size": self.size,
            "avg_price": self.avg_price,
            "direction": self.direction,
            "sl": self.sl,
            "tp": self.tp,
            "entry_time": self.entry_time,
            "holding_days": self.holding_days
        }

class PortfolioManager:
    """
    PortfolioManager handles:
    - cash and positions
    - allocation rules
    - open/close position with fee
    - SL/TP checking
    - trade logging and NAV snapshots
    """

    def __init__(self, cfg: dict, reporter=None):
        # config values
        self.cfg = cfg
        p = cfg.get("pipeline", {})
        self.init_capital = float(cfg.get("init_capital", cfg.get("pipeline", {}).get("init_capital", 1_000_000_000)))
        self.max_positions = int(p.get("topk", p.get("max_positions", 10)))
        self.min_alloc = float(p.get("min_alloc", 0.005))  # fraction of NAV
        self.execution_lag = int(p.get("execution_lag", 0))  # seconds to wait simulate execution
        # risk params from top-level config if any
        self.sl_pct = float(cfg.get("pipeline", {}).get("sl_pct", cfg.get("sl_pct", 0.01)))
        self.tp_pct = float(cfg.get("pipeline", {}).get("tp_pct", cfg.get("tp_pct", 0.04)))
        self.fee_pct = float(cfg.get("paths", {}).get("trade_fee_pct", cfg.get("trade_fee_pct", 0.0003))) if cfg.get("paths") else float(cfg.get("trade_fee_pct", 0.0003))

        # runtime state
        self.cash = float(self.init_capital)
        self.positions = {}  # ticker -> Position
        self.trade_log = []  # list of trade dicts
        self.nav_history = []  # list of {"timestamp","nav"}
        self.reporter = reporter

        logging.info(f"Portfolio initialized with capital={self.init_capital}, max_positions={self.max_positions}")

    # ---------- Core accounting ----------
    def _compute_nav(self, market_prices: dict):
        """
        market_prices: dict ticker -> {'price':float,...}
        """
        pos_val = 0.0
        for t, pos in self.positions.items():
            if t in market_prices:
                pos_val += pos.size * market_prices[t]['price'] * (1 if pos.direction == 1 else -1)
            else:
                # if no price available, estimate with avg_price
                pos_val += pos.size * pos.avg_price
        return self.cash + pos_val

    def nav(self, market_prices: dict):
        nav_val = self._compute_nav(market_prices)
        return float(nav_val)

    # ---------- Order execution (simulated) ----------
    def _simulate_execution(self, ticker, target_price):
        if self.execution_lag > 0:
            logging.debug(f"Simulating execution lag {self.execution_lag}s for {ticker}")
            time.sleep(self.execution_lag)
        # In real setup replace this with broker order & fill feedback
        return float(target_price)

    # ---------- Open / Close ----------
    def open_position(self, ticker: str, price: float, allocation_pct: float, market_prices: dict):
        """
        Open long position for ticker using allocation_pct of current NAV (fraction [0,1]).
        Returns (True, message) on success, (False, reason) on fail.
        """
        # basic checks
        if ticker in self.positions:
            return False, "Already holding"
        if len(self.positions) >= self.max_positions:
            return False, "Max positions reached"

        nav_now = self.nav(market_prices)
        alloc_vnd = nav_now * allocation_pct
        min_alloc_vnd = nav_now * self.min_alloc
        if alloc_vnd < min_alloc_vnd:
            return False, f"Allocation {alloc_vnd:.0f} < min_alloc {min_alloc_vnd:.0f}"

        # simulate execution (could use next tick price)
        exec_price = self._simulate_execution(ticker, price)
        shares = math.floor((alloc_vnd * (1 - self.fee_pct)) / exec_price)
        if shares <= 0:
            return False, "Zero shares to buy with allocation"

        cost = shares * exec_price
        fee = cost * self.fee_pct
        total_out = cost + fee
        if total_out > self.cash + 1e-9:
            return False, "Insufficient cash for order after fee"

        # create position
        sl = exec_price * (1 - self.sl_pct)
        tp = exec_price * (1 + self.tp_pct)
        pos = Position(ticker=ticker, size=shares, avg_price=exec_price, direction=1, sl=sl, tp=tp)
        self.positions[ticker] = pos
        self.cash -= total_out

        # log
        trade = {
            "timestamp": now_str(),
            "ticker": ticker,
            "side": "BUY",
            "price": exec_price,
            "size": shares,
            "cost": cost,
            "fee": fee,
            "cash_after": self.cash,
            "sl": sl,
            "tp": tp,
            "reason": "signal_open"
        }
        self.trade_log.append(trade)
        logging.info(f"OPEN {ticker} size={shares} price={exec_price:.2f} cost={cost:.0f} fee={fee:.0f} cash_after={self.cash:.0f}")

        if self.reporter:
            try:
                self.reporter.send(f"[{now_str()}] OPEN {ticker} @ {exec_price:.0f} VND, size={shares}, NAV={self.nav(market_prices):,.0f}")
            except Exception as e:
                logging.debug(f"Reporter error open_position: {e}")

        return True, None

    def close_position(self, ticker: str, price: float, market_prices: dict, reason="manual"):
        """
        Close existing position at given price (simulate execution).
        """
        if ticker not in self.positions:
            return False, "Position not found"

        pos = self.positions.pop(ticker)
        exec_price = self._simulate_execution(ticker, price)
        proceeds = pos.size * exec_price
        fee = proceeds * self.fee_pct
        net = proceeds - fee
        self.cash += net

        trade = {
            "timestamp": now_str(),
            "ticker": ticker,
            "side": "SELL",
            "price": exec_price,
            "size": pos.size,
            "proceeds": proceeds,
            "fee": fee,
            "cash_after": self.cash,
            "reason": reason
        }
        self.trade_log.append(trade)
        logging.info(f"CLOSED {ticker} size={pos.size} price={exec_price:.2f} proceeds={proceeds:.0f} fee={fee:.0f} cash_after={self.cash:.0f}")

        if self.reporter:
            try:
                self.reporter.send(f"[{now_str()}] CLOSE {ticker} @ {exec_price:.0f} VND, reason={reason}, NAV={self.nav(market_prices):,.0f}")
            except Exception as e:
                logging.debug(f"Reporter error close_position: {e}")

        return True, None

    # ---------- SL/TP checking ----------
    def check_sl_tp(self, market_prices: dict):
        """
        Iterate positions and close ones hitting SL or TP.
        Returns list of (ticker, 'SL'/'TP') closed.
        """
        closed = []
        for t, pos in list(self.positions.items()):
            if t not in market_prices:
                continue
            p = market_prices[t]['price']
            if pos.sl is not None and p <= pos.sl:
                self.close_position(t, p, market_prices, reason="SL")
                closed.append((t, "SL"))
            elif pos.tp is not None and p >= pos.tp:
                self.close_position(t, p, market_prices, reason="TP")
                closed.append((t, "TP"))
        return closed

    # ---------- Periodic housekeeping ----------
    def snapshot(self, market_prices: dict):
        """Record NAV snapshot and optionally send periodic report."""
        n = self.nav(market_prices)
        self.nav_history.append({"timestamp": now_str(), "nav": n})
        logging.info(f"NAV snapshot {n:,.0f}")
        return n

    def increment_holding_days(self):
        for pos in self.positions.values():
            pos.holding_days += 1

    # ---------- Utility helpers ----------
    def top_available_allocation(self, market_prices: dict):
        """
        Determine allocation fraction per new position.
        Simple rule: equal-split of free capital across available slots,
        but do not go below min_alloc.
        Returns allocation_pct (fraction) usable by open_position.
        """
        nav_now = self.nav(market_prices)
        free = self.cash
        slots = max(1, self.max_positions - len(self.positions))
        if slots <= 0:
            return 0.0
        alloc_per = (free / slots) / nav_now  # fraction of NAV
        if alloc_per < self.min_alloc:
            return 0.0
        return float(alloc_per)

    def save_trade_log(self, path="trade_log.csv"):
        if len(self.trade_log) == 0:
            logging.info("No trades to save.")
            return
        df = pd.DataFrame(self.trade_log)
        df.to_csv(path, index=False)
        logging.info(f"Trade log saved to {path}")

    def save_nav_history(self, path="nav_history.csv"):
        if len(self.nav_history) == 0:
            logging.info("No NAV history to save.")
            return
        pd.DataFrame(self.nav_history).to_csv(path, index=False)
        logging.info(f"NAV history saved to {path}")

    # ---------- Convenience: show positions ----------
    def positions_df(self):
        rows = [p.to_dict() for p in self.positions.values()]
        if not rows:
            return pd.DataFrame(columns=["ticker","size","avg_price","sl","tp","entry_time","holding_days"])
        return pd.DataFrame(rows)

# End of Part 4
# Part 5: Signal Engine (Rule-based + RL ensemble)
# Requirements: numpy, pandas, logging
# Uses: FeatureEngineer (Part 2), A3CAgent & DDPGAgent (Part 3), PortfolioManager (Part 4), DataLayer (Part 1)

import numpy as np
import pandas as pd
import logging
from typing import List, Tuple, Dict

TS_FMT = "%Y-%m-%d %H:%M"

class SignalEngine:
    """
    Generate trading signals by:
    - computing rule-based scores per ticker (RSI, volume spike, SMA crossover)
    - querying RL agents (A3C, DDPG) for predictions
    - combining rule scores and RL votes into final 'score' and action
    - returning top-k buy/sell candidates (action in {-1,0,1})
    """

    def __init__(self, cfg: dict, feature_engineer, a3c_agent, ddpg_agent, reporter=None):
        self.cfg = cfg
        self.fe = feature_engineer
        self.a3c = a3c_agent
        self.ddpg = ddpg_agent
        self.reporter = reporter

        p = cfg.get("pipeline", {})
        self.rsi_buy_th = float(p.get("rsi_buy_threshold", 35))
        self.vol_spike_mult = float(p.get("vol_spike_mult", 1.5))
        self.rule_min_score = int(p.get("rule_min_score", 3))
        self.topk = int(p.get("topk", 10))
        self.lookback = int(p.get("state_lookback", 10))

    # ---------------------------
    # Rule scoring helpers
    # ---------------------------
    def score_rsi(self, df: pd.DataFrame) -> int:
        """Return +1 if RSI indicates oversold (buy), -1 if overbought (sell), else 0"""
        if "rsi_14" not in df.columns:
            return 0
        rsi = df["rsi_14"].iloc[-1]
        if np.isnan(rsi):
            return 0
        if rsi <= self.rsi_buy_th:
            return +1
        # optional: overbought threshold 70 -> sell
        if rsi >= 70:
            return -1
        return 0

    def score_vol_spike(self, df: pd.DataFrame) -> int:
        """+1 if latest volume > rolling_mean * vol_spike_mult"""
        if "volume" not in df.columns:
            return 0
        window = min(len(df), max(5, self.lookback))
        recent = df["volume"].iloc[-window:]
        mean_v = recent.mean() if len(recent) > 0 else 0.0
        latest = df["volume"].iloc[-1]
        if mean_v > 0 and latest > mean_v * self.vol_spike_mult:
            return +1
        return 0

    def score_sma_crossover(self, df: pd.DataFrame) -> int:
        """Detect SMA20 crossover: +1 if close crosses above sma20; -1 if crosses below; else 0"""
        if "sma_20" not in df.columns:
            return 0
        if len(df) < 2:
            return 0
        prev_close = df["close"].iloc[-2]
        curr_close = df["close"].iloc[-1]
        prev_sma = df["sma_20"].iloc[-2]
        curr_sma = df["sma_20"].iloc[-1]
        if prev_close < prev_sma and curr_close > curr_sma:
            return +1
        if prev_close > prev_sma and curr_close < curr_sma:
            return -1
        return 0

    # ---------------------------
    # Combine rules + RL
    # ---------------------------
    def compute_rule_score(self, df: pd.DataFrame) -> int:
        """Sum of individual rule scores (positive indicates buy-likely)"""
        s = 0
        s += self.score_rsi(df)
        s += self.score_vol_spike(df)
        s += self.score_sma_crossover(df)
        return int(s)

    def rl_votes(self, state_vec: np.ndarray) -> Tuple[int, int]:
        """
        Query A3C and DDPG agents.
        Returns tuple (a3c_action, ddpg_action) each in {-1,0,1}
        """
        try:
            a1 = self.a3c.predict(state_vec)
        except Exception as e:
            logging.warning(f"A3C predict error: {e}")
            a1 = 0
        try:
            a2 = self.ddpg.predict(state_vec)
        except Exception as e:
            logging.warning(f"DDPG predict error: {e}")
            a2 = 0
        return int(a1), int(a2)

    def score_from_votes(self, vote_a3c: int, vote_ddpg: int) -> int:
        """
        Convert agent votes to a pseudo-score:
        - each buy=+1, sell=-1, hold=0
        - sum weighted (equal weights) => -2..+2
        """
        return int(vote_a3c + vote_ddpg)

    # ---------------------------
    # Main entry: generate signals
    # ---------------------------
    def generate_signals(self, tickers: List[str], data_store: Dict[str, pd.DataFrame],
                         portfolio=None) -> List[Dict]:
        """
        Generate signals for provided tickers.
        Inputs:
            - tickers: list of tickers to evaluate (VN30 universe)
            - data_store: dict[ticker -> pd.DataFrame] (historical + realtime appended)
            - portfolio: PortfolioManager instance (optional) used to skip already-held tickers
        Returns:
            - signals: list of dicts sorted by descending score, each dict:
                {"ticker":..., "action": -1/0/1, "score": int, "rule_score": int, "rl_score": int, "price": float}
        """
        results = []
        for t in tickers:
            df = data_store.get(t, None)
            if df is None or len(df) < max(5, self.lookback):
                continue

            # Ensure TA present (if not, compute via feature engineer)
            # FeatureEngineer.add_ta_indicators will fill needed columns
            try:
                df_ta = self.fe.add_ta_indicators(df)
            except Exception as e:
                logging.debug(f"FE add_ta failed for {t}: {e}")
                continue

            rule_score = self.compute_rule_score(df_ta)

            # Build state vector from last lookback rows
            state = self.fe.make_state(df_ta)
            if state is None:
                continue

            a3c_vote, ddpg_vote = self.rl_votes(state)
            rl_score = self.score_from_votes(a3c_vote, ddpg_vote)  # range -2..2

            # Combined score: simple sum (rule_score + rl_score)
            combined = int(rule_score + rl_score)

            latest_price = float(df_ta["close"].iloc[-1])

            # Final action decision:
            # - require rule_score >= rule_min_score OR rl_score strong enough
            action = 0
            if rule_score >= self.rule_min_score:
                action = 1
            elif rl_score >= 2 and rule_score >= 0:
                action = 1
            elif rl_score <= -2 and rule_score <= 0:
                action = -1
            else:
                # if combined positive enough
                if combined >= self.rule_min_score:
                    action = 1
                elif combined <= -self.rule_min_score:
                    action = -1
                else:
                    action = 0

            # Skip if already hold (unless sell signal)
            if portfolio is not None and t in getattr(portfolio, "positions", {}):
                if action == 1:
                    # already held, skip buy
                    continue

            results.append({
                "ticker": t,
                "action": int(action),
                "combined_score": int(combined),
                "rule_score": int(rule_score),
                "rl_score": int(rl_score),
                "a3c_vote": int(a3c_vote),
                "ddpg_vote": int(ddpg_vote),
                "price": latest_price,
                "timestamp": df_ta["timestamp"].iloc[-1].strftime(TS_FMT) if hasattr(df_ta["timestamp"].iloc[-1], "strftime") else str(df_ta["timestamp"].iloc[-1])
            })

        # Post-filter: keep only buy/sell signals (non-zero), sort by combined_score desc, then return topk
        nonzero = [r for r in results if r["action"] != 0]
        # Sort buys highest combined_score descending; sells we may want to prioritize by negative score magnitude
        buys = [r for r in nonzero if r["action"] == 1]
        sells = [r for r in nonzero if r["action"] == -1]

        buys_sorted = sorted(buys, key=lambda x: (x["combined_score"], x["rl_score"]), reverse=True)
        sells_sorted = sorted(sells, key=lambda x: (abs(x["combined_score"]), abs(x["rl_score"])), reverse=True)

        top_buys = buys_sorted[: self.topk]
        top_sells = sells_sorted[: self.topk]

        signals = top_buys + top_sells

        # Optional: reporter summary
        if self.reporter and signals:
            try:
                msg_lines = [f"[{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}] Signals generated: {len(signals)}"]
                for s in signals:
                    msg_lines.append(f"{s['ticker']} act={s['action']} comb={s['combined_score']} rule={s['rule_score']} rl={s['rl_score']} price={s['price']:.0f}")
                self.reporter.send("\n".join(msg_lines))
            except Exception as e:
                logging.debug(f"Reporter error in generate_signals: {e}")

        return signals
# Part 6: Reporter (Telegram + Google Sheets)
# Yêu cầu: requests, gspread, oauth2client (pip install gspread oauth2client)

import logging
import requests
import pandas as pd

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
except ImportError:
    gspread = None
    ServiceAccountCredentials = None

TS_FMT = "%Y-%m-%d %H:%M"

class Reporter:
    def __init__(self, cfg: dict):
        # Telegram setup
        tele_cfg = cfg.get("telegram", {})
        self.token = tele_cfg.get("bot_token")
        self.chat_id = tele_cfg.get("chat_id")
        self.thread_id = tele_cfg.get("message_thread_id", None)
        self.tele_base = f"https://api.telegram.org/bot{self.token}" if self.token else None

        # Google Sheet setup
        g_cfg = cfg.get("google", {})
        self.gs_client = None
        self.sheet = None
        if gspread and g_cfg.get("service_account_json") and g_cfg.get("sheet_id"):
            try:
                scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
                creds = ServiceAccountCredentials.from_json_keyfile_name(g_cfg["service_account_json"], scope)
                self.gs_client = gspread.authorize(creds)
                self.sheet = self.gs_client.open_by_key(g_cfg["sheet_id"]).worksheet(g_cfg["sheet_name"])
                logging.info(f"Connected to Google Sheet: {g_cfg['sheet_name']}")
            except Exception as e:
                logging.warning(f"Google Sheet init failed: {e}")

    # -----------------------
    # Telegram functions
    # -----------------------
    def send_telegram(self, text: str):
        if not self.tele_base:
            return
        try:
            url = f"{self.tele_base}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text
            }
            if self.thread_id:
                payload["message_thread_id"] = self.thread_id
            r = requests.post(url, data=payload, timeout=5)
            if r.status_code != 200:
                logging.warning(f"Telegram send failed: {r.text}")
        except Exception as e:
            logging.warning(f"Telegram error: {e}")

    # -----------------------
    # Google Sheet functions
    # -----------------------
    def append_to_sheet(self, values: list):
        """
        Append a row to Google Sheet.
        values: list of strings/numbers
        """
        if not self.sheet:
            return
        try:
            self.sheet.append_row(values, value_input_option="USER_ENTERED")
        except Exception as e:
            logging.warning(f"Google Sheet append error: {e}")

    # -----------------------
    # High-level wrappers
    # -----------------------
    def report_signal(self, signals: list):
        """
        Send summary of signals to Telegram and Google Sheet
        signals: list of dicts (output from SignalEngine.generate_signals)
        """
        if not signals:
            return
        # Telegram summary
        lines = [f"📊 Signals ({len(signals)} tickers):"]
        for s in signals:
            lines.append(f"{s['ticker']} | act={s['action']} | comb={s['combined_score']} | price={s['price']:.0f}")
        self.send_telegram("\n".join(lines))

        # Google Sheet log
        if self.sheet:
            for s in signals:
                row = [
                    s["timestamp"], s["ticker"], s["action"],
                    s["combined_score"], s["rule_score"], s["rl_score"],
                    s["a3c_vote"], s["ddpg_vote"], s["price"]
                ]
                self.append_to_sheet(row)

    def report_trade(self, trade: dict):
        """
        Report a single trade execution (open/close).
        trade: dict (from PortfolioManager.trade_log)
        """
        text = f"💹 TRADE {trade['side']} {trade['ticker']} @ {trade['price']:.0f}, size={trade['size']}"
        if "reason" in trade:
            text += f" ({trade['reason']})"
        self.send_telegram(text)

        if self.sheet:
            row = [
                trade.get("timestamp"), trade.get("ticker"),
                trade.get("side"), trade.get("price"),
                trade.get("size"), trade.get("reason", "")
            ]
            self.append_to_sheet(row)

    def report_nav(self, nav: float, positions: dict):
        """
        Report NAV snapshot and open positions
        """
        pos_summary = ", ".join([f"{t}:{p.size}@{p.avg_price:.0f}" for t,p in positions.items()])
        text = f"📈 NAV={nav:,.0f} | Positions: {pos_summary if pos_summary else 'None'}"
        self.send_telegram(text)

        if self.sheet:
            row = [pd.Timestamp.now().strftime(TS_FMT), nav, pos_summary]
            self.append_to_sheet(row)
# Part 7: Main Loop (Realtime 1 phút)

import time
import logging
import pandas as pd

TS_FMT = "%Y-%m-%d %H:%M"

def main_loop(cfg, client, data_layer, feature_engineer,
              a3c_agent, ddpg_agent,
              portfolio, signal_engine, reporter):
    """
    Main realtime loop:
    - Every snapshot_seconds, update data, generate signals, execute trades,
      check SL/TP, report NAV
    """

    snap_seconds = int(cfg["pipeline"].get("snapshot_seconds", 60))

    logging.info("🚀 Starting realtime trading loop...")
    reporter.send_telegram("🚀 Realtime trading loop started.")

    while True:
        try:
            # 1. Lấy danh sách VN30 hiện tại
            tickers = data_layer.tickers

            # 2. Sinh tín hiệu
            signals = signal_engine.generate_signals(
                tickers=tickers,
                data_store=data_layer.data_store,
                portfolio=portfolio
            )

            # 3. Thực hiện giao dịch theo tín hiệu
            market_prices = {}
            for t in tickers:
                df = data_layer.data_store.get(t)
                if df is not None and len(df) > 0:
                    latest_price = float(df["close"].iloc[-1])
                    market_prices[t] = {"price": latest_price}

            for sig in signals:
                t = sig["ticker"]
                act = sig["action"]
                px = sig["price"]

                if act == 1:
                    alloc_pct = portfolio.top_available_allocation(market_prices)
                    if alloc_pct > 0:
                        ok, reason = portfolio.open_position(t, px, alloc_pct, market_prices)
                        if ok:
                            reporter.report_trade(portfolio.trade_log[-1])
                        else:
                            logging.info(f"Cannot open {t}: {reason}")
                elif act == -1 and t in portfolio.positions:
                    ok, reason = portfolio.close_position(t, px, market_prices, reason="signal_close")
                    if ok:
                        reporter.report_trade(portfolio.trade_log[-1])

            # 4. Kiểm tra SL/TP
            closed = portfolio.check_sl_tp(market_prices)
            for t, why in closed:
                reporter.report_trade(portfolio.trade_log[-1])

            # 5. Snapshot NAV
            nav = portfolio.snapshot(market_prices)
            reporter.report_nav(nav, portfolio.positions)

            # 6. Ngủ đến lần tiếp theo
            logging.info(f"Sleeping {snap_seconds} seconds...\n")
            time.sleep(snap_seconds)

        except KeyboardInterrupt:
            logging.info("⏹️ Loop stopped by user")
            break
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            time.sleep(snap_seconds)
if __name__ == "__main__":
    # 1. Init system (Block 0)
    cfg, client, tele_reporter = init_all("config.json")

    # 2. Data Layer (Block 1)
    data_layer = DataLayer(client, cfg, reporter=tele_reporter)
    data_layer.load_historical("vnindex_price_fa_merged.csv")
    data_layer.start_realtime()

    # 3. Feature Engineering (Block 2)
    fe = FeatureEngineer(client, cfg)

   # 4. RL Models (Block 3)
    a3c_agent, ddpg_agent = init_agents(cfg, data_layer.data_store)

    # 5. Portfolio Manager (Block 4)
    portfolio = PortfolioManager(cfg, reporter=tele_reporter)

    # 6. Signal Engine (Block 5)
    signal_engine = SignalEngine(cfg, fe, a3c_agent, ddpg_agent, reporter=tele_reporter)

    # 7. Reporter (Block 6)
    reporter = Reporter(cfg)

    # 8. Main Loop (Block 7)
    main_loop(cfg, client, data_layer, fe,
              a3c_agent, ddpg_agent,
              portfolio, signal_engine, reporter)
