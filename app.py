import streamlit as st
from supabase import create_client, Client
from datetime import datetime, timezone, timedelta
from gtts import gTTS
import io
import re
import pytz
import json
import random
from langchain_groq import ChatGroq
from langchain_community.embeddings import HuggingFaceEmbeddings

# ==========================================
# 1. 系統初始化與雲端連線
# ==========================================
st.set_page_config(page_title="NOVA | Agentic 時事單字庫", page_icon="🌍", layout="centered")

@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

@st.cache_resource
def init_embeddings():
    # 延遲載入並快取向量模型，避免每次對話都重新載入
    return HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

supabase = init_supabase()
embeddings_model = init_embeddings()

# 設定 Router 總機模型 (使用 70B 確保 JSON 格式與意圖判斷 100% 準確)
llm = ChatGroq(
    api_key=st.secrets["GROQ_API_KEY"], # 👉 明確傳入金鑰
    model="llama-3.3-70b-versatile", 
    temperature=0
)

# ==========================================
# 2. 核心大腦與資料庫操作函數
# ==========================================

def router_agent(user_input):
    """🧠 大腦決策：判斷使用者意圖並輸出 JSON 指令"""
    prompt = f"""你是一個英語學習系統的路由大腦。請分析使用者輸入，判斷他們的意圖。
    
    【可選任務清單】：
    1. "search_example": 尋找特定情境、語氣、用法、或中文意思的單字 (例如: 商業談判、焦慮、環保)。
    2. "search_news": 尋找特定新聞事件、政治人物、公司、時事 (例如: AI法規、台積電)。
    3. "srs_review": 使用者想複習單字、測驗、或直接說「今天該背單字了」。
    
    【輸出規範】：只能輸出純 JSON，格式如下：
    {{
        "action": "上述三個任務之一",
        "query": "如果是 search 任務，請將意圖濃縮成適合轉化為向量的『英文關鍵字』(例如: business negotiation)。如果是 srs_review 則填 null。"
    }}
    
    使用者輸入：{user_input}"""
    
    try:
        res = llm.invoke(prompt)
        clean_json = res.content.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_json)
    except Exception as e:
        print(f"Router 發生錯誤: {e}")
        # 預設降級為 SRS 複習模式
        return {"action": "srs_review", "query": None}

def fetch_srs_sample(limit=15):
    """📅 SRS 複習模式：過期優先 + 隨機抽樣"""
    tw_tz = pytz.timezone('Asia/Taipei')
    today = datetime.now(tw_tz).date().strftime("%Y-%m-%d")
    
    # 1. 抓取過期單字
    res = supabase.table("user_srs_progress").select("word").lte("due_date", today).execute()
    overdue_words = [p['word'] for p in res.data]
    
    # 2. 隨機抽樣
    target_words = random.sample(overdue_words, limit) if len(overdue_words) > limit else overdue_words
    
    if not target_words: return []
    
    # 3. 抓取單字詳細內容與進度
    cards_res = supabase.table("llm_generation_cache").select("*").in_("word", target_words).execute()
    progress_res = supabase.table("user_srs_progress").select("*").in_("word", target_words).execute()
    user_progress = {p['word']: p for p in progress_res.data}
    
    final_cards = []
    for c in cards_res.data:
        c['srs'] = user_progress.get(c['word'])
        final_cards.append(c)
    
    # 打亂順序，增加測驗隨機性
    random.shuffle(final_cards)
    return final_cards

def semantic_search(query_text, mode="example"):
    """🔍 RAG 搜尋模式：透過向量搜尋對應大腦"""
    query_vec = embeddings_model.embed_query(query_text)
    
    rpc_name = "match_examples" if mode == "example" else "match_contexts"
    res = supabase.rpc(rpc_name, {
        "query_embedding": query_vec,
        "match_threshold": 0.3, # 可調整嚴格度
        "match_count": 10       # 每次搜尋最多秀 10 個字
    }).execute()
    
    if not res.data: return []
    
    # 將搜尋結果與使用者的 SRS 進度結合
    words = [r['word'] for r in res.data]
    progress_res = supabase.table("user_srs_progress").select("*").in_("word", words).execute()
    user_progress = {p['word']: p for p in progress_res.data}
    
    for c in res.data:
        c['srs'] = user_progress.get(c['word'])
    return res.data

def update_srs_progress(word, quality, current_srs):
    """📈 更新 SRS 熟悉度權重 (維持原版 SM-2 演算法)"""
    ease_factor = current_srs['ease_factor'] if current_srs else 2.5
    interval = current_srs['interval'] if current_srs else 0
    
    if quality < 3:
        interval = 1 
    else:
        if interval == 0:
            interval = 1
        elif interval == 1:
            interval = 6
        else:
            interval = round(interval * ease_factor)
            
    ease_factor = ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ease_factor = max(1.3, ease_factor) 
    
    next_due_date = (datetime.now(timezone.utc) + timedelta(days=interval)).strftime("%Y-%m-%d")
    
    data = {
        "word": word,
        "ease_factor": ease_factor,
        "interval": interval,
        "due_date": next_due_date,
        "last_reviewed": datetime.now(timezone.utc).isoformat()
    }
    supabase.table("user_srs_progress").upsert(data, on_conflict="word").execute()

def text_to_speech_player(text):
    """🔊 文字轉語音播放器"""
    tts = gTTS(text=text, lang='en', slow=False)
    audio_bytes = io.BytesIO()
    tts.write_to_fp(audio_bytes)
    st.audio(audio_bytes.getvalue(), format='audio/mp3')

def get_part(text, start, end=None):
    """✂️ Markdown 萃取工具"""
    if start not in text: return ""
    s = text.find(start) + len(start)
    if end and end in text[s:]:
        e = text.find(end, s)
        return text[s:e].strip()
    return text[s:].strip()

# ==========================================
# 3. Streamlit 前端 UI 介面
# ==========================================
st.title("🌍 NOVA 智能時事單字庫")
st.markdown("*Agentic News-Driven Vocabulary Builder (ANDVB)*")

# --- 側邊欄控制 ---
with st.sidebar:
    st.header("⚙️ 系統狀態")
    current_count = len(st.session_state.get('due_cards', []))
    st.caption(f"📌 目前載入任務數：{current_count} 題")
    
    if st.button("🔄 載入今日 SRS 任務", use_container_width=True):
        st.session_state.due_cards = fetch_srs_sample(limit=15)
        st.session_state.current_index = 0
        st.session_state.card_flipped = False
        st.rerun()

# --- 狀態初始化 ---
if 'due_cards' not in st.session_state: st.session_state.due_cards = []
if 'current_index' not in st.session_state: st.session_state.current_index = 0
if 'card_flipped' not in st.session_state: st.session_state.card_flipped = False

# --- 🤖 核心對話輸入框 (觸發 Agent) ---
user_query = st.chat_input("想找什麼情境的單字？或是輸入「我要複習」、「跟戰爭有關的新聞」...")

if user_query:
    with st.spinner("🤖 總機判斷意圖中..."):
        decision = router_agent(user_query)
        
        if decision['action'] == 'srs_review':
            st.session_state.due_cards = fetch_srs_sample(limit=15)
            st.toast("🎯 總機為您準備了今天的複習清單！")
        else:
            mode = "example" if decision['action'] == "search_example" else "news"
            st.session_state.due_cards = semantic_search(decision['query'], mode=mode)
            if st.session_state.due_cards:
                st.toast(f"🔍 總機找到了 {len(st.session_state.due_cards)} 個適合的單字！")
            else:
                st.warning("查無相關單字，請嘗試其他關鍵字。")
        
        # 載入新任務後重置索引
        st.session_state.current_index = 0
        st.session_state.card_flipped = False

st.divider()

# --- 🗂️ 渲染單字卡區域 ---
if not st.session_state.due_cards:
    st.info("👋 歡迎使用 NOVA！請在下方輸入你想尋找的單字情境，或點擊側邊欄開始今日複習。")
elif st.session_state.current_index >= len(st.session_state.due_cards):
    st.balloons()
    st.success("🎉 太棒了！你已經清空了這批任務！繼續搜尋或明天再來吧！")
    if st.button("🔄 再來一批 SRS 複習"):
        st.session_state.due_cards = fetch_srs_sample(limit=15)
        st.session_state.current_index = 0
        st.rerun()
else:
    total_cards = len(st.session_state.due_cards)
    current_num = st.session_state.current_index + 1
    st.progress(current_num / total_cards, text=f"當前進度：{current_num} / {total_cards}")
    
    current_card = st.session_state.due_cards[st.session_state.current_index]
    target_word = current_card['word']
    
    # 判斷是否為片語
    match = re.search(r"📌 \*\*焦點詞彙\*\*：\*\*(.*?)\*\*", current_card.get('teacher_card_content', ''))
    actual_focus_phrase = match.group(1).strip() if match else target_word
    
    st.markdown("### 📰 閱讀新聞，猜猜這是什麼字？")
    
    # 1. 顯示挖空的新聞原句 (確保在按鈕上方)
    context_masked = re.sub(re.escape(actual_focus_phrase), " **[_____]** ", current_card.get('news_context', ''), flags=re.IGNORECASE)
    st.info(context_masked)
    
    # 2. 翻牌按鈕
    if not st.session_state.card_flipped:
        if st.button("👁️ 翻開記憶卡看答案", key=f"flip_{target_word}_{current_num}", use_container_width=True):
            st.session_state.card_flipped = True
            st.rerun()
            
    # 3. 翻牌後的單字卡內容
    if st.session_state.card_flipped:
        st.divider()
        content = current_card.get('teacher_card_content', '')
        
        # --- 區塊 1：新聞原句 ---
        st.markdown("### 📖 時事單字記憶卡")
        news_en = get_part(content, "📰 **新聞原句**：", "📰 **中文翻譯**：").strip().strip('"')
        news_zh = get_part(content, "📰 **中文翻譯**：", "📌")
        
        st.markdown("##### 📰 新聞原句 News Context")
        st.write(news_en)
        if news_en:
            text_to_speech_player(news_en)
        if news_zh: st.write(news_zh)
        
        st.divider()

        # --- 區塊 2：焦點詞彙 ---
        st.markdown("### 📌 焦點詞彙與解釋")
        vocab_info = get_part(content, "📌", "💡")
        if vocab_info:
            st.info(f"**{target_word}**\n\n{vocab_info}")
        
        st.divider()

        # --- 區塊 3：生活例句 ---
        st.markdown("### 💡 生活情境造句 Daily Life Example")
        example_en = get_part(content, "**🇺🇸**：", "**🇹🇼**：")
        example_zh = get_part(content, "**🇹🇼**：")
        
        col_en, col_zh = st.columns(2)
        with col_en:
            st.markdown("##### 🇺🇸 English")
            st.info(example_en)
            if example_en:
                text_to_speech_player(example_en)
        with col_zh:
            st.markdown("##### 🇹🇼 中文翻譯")
            st.success(example_zh)

        # --- 區塊 4：魔王考官測驗題 (收合在下方) ---
        st.divider()
        if current_card.get('examiner_quiz_content'):
            with st.expander("🕵️‍♂️ [選作] 點我挑戰魔王考官測驗題"):
                st.markdown(current_card['examiner_quiz_content'])

        st.divider()
        
        # --- 區塊 5：SRS 回饋按鈕 ---
        st.markdown("##### 🧠 這個單字你熟悉嗎？(點擊紀錄進度並換下一題)")
        col1, col2, col3, col4 = st.columns(4)
        
        def handle_feedback(quality):
            update_srs_progress(target_word, quality, current_card.get('srs'))
            st.session_state.current_index += 1
            st.session_state.card_flipped = False
            
        with col1:
            if st.button("😭 忘記了", key=f"btn0_{target_word}", use_container_width=True): handle_feedback(0); st.rerun()
        with col2:
            if st.button("😓 很吃力", key=f"btn1_{target_word}", use_container_width=True): handle_feedback(1); st.rerun()
        with col3:
            if st.button("🙂 背起來", key=f"btn2_{target_word}", use_container_width=True): handle_feedback(4); st.rerun()
        with col4:
            if st.button("😎 太簡單", key=f"btn3_{target_word}", use_container_width=True): handle_feedback(5); st.rerun()