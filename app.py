import streamlit as st
from supabase import create_client, Client
from datetime import datetime, timezone, timedelta
from gtts import gTTS
from typing import Optional
import io
import re
import pytz
import random

# LangChain 相關
from langchain_groq import ChatGroq
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

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
    return HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

supabase = init_supabase()
embeddings_model = init_embeddings()

llm = ChatGroq(
    api_key=st.secrets["GROQ_API_KEY"],
    model="llama-3.3-70b-versatile", 
    temperature=0
)

# ==========================================
# 2. 核心大腦與資料庫操作函數
# ==========================================

# 🧠 定義總機輸出的嚴格結構
class IntentRoute(BaseModel):
    reasoning: str = Field(description="分析使用者的意圖。")
    action: str = Field(description="必須是：'search_word', 'search_example', 'search_news', 'srs_review', 'learn_new'")
    query: Optional[str] = Field(default="", description="若是 search 任務，將輸入語句轉換為英文關鍵字；否則請絕對填入空字串 \"\"")
    srs_filter: str = Field(default="all", description="過濾條件：'new' (只要未讀新字), 'review' (只要需複習舊字), 'all' (不限制)")

router_llm = llm.with_structured_output(IntentRoute)

def router_agent(user_input):
    """🧠 純 LLM 大腦決策 (透過側邊欄按鈕分流後，對話框全交給 LLM 處理複雜交集)"""
    prompt = ChatPromptTemplate.from_messages([
        ("system", """你是一個英語學習系統的路由大腦。請分析使用者輸入判斷意圖。
        
        【任務清單】:
        1. "search_word": 使用者想要直接查詢「特定單字」，或者尋找「特定中文意思、同義詞」的單字 (例如: "查詢 apple", "有沒有表示減輕的字", "類似 angry 的字")。
        2. "search_example": 尋找特定情境、語境、生活對話 (例如: "商業談判", "辦公室閒聊")。
        3. "search_news": 尋找特定新聞事件、時事、政治 (例如: "AI法規", "台積電")。
        4. "learn_new": 單純說「教我新字」，無特定情境。
        5. "srs_review": 單純說「我要複習」，無特定情境。
        
        【過濾器 srs_filter 判斷鐵則】:
        - 如果使用者說「我想找辦公室文化，且只要沒看過的新字」 -> action="search_example", query="office culture", srs_filter="new"
        - 如果使用者說「考我幾個跟戰爭有關的字」 -> action="search_news", query="war", srs_filter="review"
        - 沒特別提就用 "all"。
        """),
        ("user", "{input}")
    ])
    
    try:
        chain = prompt | router_llm
        result = chain.invoke({"input": user_input})
        print(f"🧠 [總機內心戲]: {result.reasoning} | Filter: {result.srs_filter}") 
        return {"action": result.action, "query": result.query, "srs_filter": result.srs_filter}
    except Exception as e:
        print(f"Router 發生錯誤: {e}")
        return {"action": "srs_review", "query": None, "srs_filter": "all"}

def fetch_srs_words(mode="review", limit=15):
    """📅 取詞工具：支援「複習舊字 (review)」與「學習新字 (new)」"""
    tw_tz = pytz.timezone('Asia/Taipei')
    today_iso = datetime.now(tw_tz).isoformat()
    
    query = supabase.table("user_srs_progress").select("word")
    
    # 根據 schema 判斷是新字還是舊字
    if mode == "new":
        # 新單字：從未複習過 (repetition_count == 0)
        query = query.eq("repetition_count", 0)
    else:
        # 複習單字：已經學過且時間到了
        query = query.gt("repetition_count", 0).lte("next_review_date", today_iso)
        
    res = query.execute()
    candidate_words = [p['word'] for p in res.data]
    
    target_words = random.sample(candidate_words, limit) if len(candidate_words) > limit else candidate_words
    if not target_words: return []
    
    # 抓取詳細卡片與進度
    cards_res = supabase.table("llm_generation_cache").select("*").in_("word", target_words).execute()
    progress_res = supabase.table("user_srs_progress").select("*").in_("word", target_words).execute()
    user_progress = {p['word']: p for p in progress_res.data}
    
    final_cards = []
    for c in cards_res.data:
        c['srs'] = user_progress.get(c['word'])
        final_cards.append(c)
    
    random.shuffle(final_cards)
    return final_cards

def semantic_search(query_text, mode="example", srs_filter="all", limit=10, threshold=0.55):
    """🔍 RAG 搜尋模式 + SRS 交集過濾"""
    query_vec = embeddings_model.embed_query(query_text)
    
    if mode == "example":
        rpc_name = "match_examples"
    elif mode == "news":
        rpc_name = "match_contexts"
    else:
        rpc_name = "match_words"
        
    # 1. 透過向量搜尋取得最接近的單字清單 (只拿到 word 和相似度)
    res = supabase.rpc(rpc_name, {
        "query_embedding": query_vec, 
        "match_threshold": threshold,
        "match_count": 30
    }).execute()
    
    if not res.data: return []
    words = [r['word'] for r in res.data]
    
    # 2. 去教材庫把完整的單字卡內容抓出來
    cards_res = supabase.table("llm_generation_cache").select("*").in_("word", words).execute()
    card_data_map = {c['word']: c for c in cards_res.data}
    
    # 3. 抓取這些字的 SRS 進度
    progress_res = supabase.table("user_srs_progress").select("*").in_("word", words).execute()
    user_progress = {p['word']: p for p in progress_res.data}
    
    tw_tz = pytz.timezone('Asia/Taipei')
    today_iso = datetime.now(tw_tz).isoformat()
    
    final_cards = []
    for c in res.data:
        word = c['word']
        srs_data = user_progress.get(word)
        
        # 預防沒有進度紀錄的孤兒單字
        rep_count = srs_data.get('repetition_count', 0) if srs_data else 0
        next_date = srs_data.get('next_review_date', '2099-01-01') if srs_data else '2099-01-01'
        
        # 🌟 交集過濾邏輯
        if srs_filter == "new" and rep_count != 0:
            continue # 不要舊字
        if srs_filter == "review" and (rep_count == 0 or next_date > today_iso):
            continue # 不要新字，且時間必須到期
            
        # 🌟 組合完整的卡片資料 (將原本的空殼填入完整資料)
        full_card = card_data_map.get(word, {})
        full_card['word'] = word
        full_card['srs'] = srs_data
        final_cards.append(full_card)
        
        if len(final_cards) >= limit:
            break
            
    return final_cards

def update_srs_progress(word, quality, current_srs):
    """📈 更新 SRS：對齊新版 user_srs_progress 欄位命名"""
    # 讀取現有狀態，預設值對應新 schema
    easiness_factor = current_srs['easiness_factor'] if current_srs else 2.5
    interval = current_srs['interval'] if current_srs else 0
    repetition_count = current_srs.get('repetition_count', 0) if current_srs else 0
    
    if quality < 3:
        interval = 1 
        repetition_count = 0 # 答錯歸零
    else:
        repetition_count += 1
        if repetition_count == 1:
            interval = 1
        elif repetition_count == 2:
            interval = 6
        else:
            interval = round(interval * easiness_factor)
            
    easiness_factor = easiness_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    easiness_factor = max(1.3, easiness_factor) 
    
    next_due_date = (datetime.now(timezone.utc) + timedelta(days=interval)).isoformat()
    
    data = {
        "word": word,
        "easiness_factor": easiness_factor,
        "interval": interval,
        "repetition_count": repetition_count,
        "next_review_date": next_due_date,
        "last_reviewed_at": datetime.now(timezone.utc).isoformat()
    }
    supabase.table("user_srs_progress").upsert(data, on_conflict="word").execute()

def text_to_speech_player(text):
    tts = gTTS(text=text, lang='en', slow=False)
    audio_bytes = io.BytesIO()
    tts.write_to_fp(audio_bytes)
    st.audio(audio_bytes.getvalue(), format='audio/mp3')

def get_part(text, start, end=None):
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
    st.header("⚙️ 系統狀態與快速導航")
    current_count = len(st.session_state.get('due_cards', []))
    st.caption(f"📌 目前載入任務數：{current_count} 題")
    
    st.divider()
    st.markdown("### 🚀 快速開始")
    
    # 🌟 實體按鈕 1：學習全新單字
    if st.button("🆕 抽取 10 個新單字", use_container_width=True):
        st.session_state.due_cards = fetch_srs_words(mode="new", limit=10)
        st.session_state.current_index = 0
        st.session_state.card_flipped = False
        st.rerun()

    # 🌟 實體按鈕 2：複習今日到期單字
    if st.button("🔄 開始今日 SRS 複習", use_container_width=True):
        st.session_state.due_cards = fetch_srs_words(mode="review", limit=15)
        st.session_state.current_index = 0
        st.session_state.card_flipped = False
        st.rerun()

# --- 狀態初始化 ---
if 'due_cards' not in st.session_state: st.session_state.due_cards = []
if 'current_index' not in st.session_state: st.session_state.current_index = 0
if 'card_flipped' not in st.session_state: st.session_state.card_flipped = False

# --- 🤖 核心對話輸入框 ---
user_query = st.chat_input("輸入你想找的情境 (例如：考我幾個跟科技有關的舊字)...")

if user_query:
    with st.spinner("🤖 總機判斷意圖中..."):
        decision = router_agent(user_query)
        st.write(f"🕵️‍♂️ [Debug] {decision}") # 留著聽診器方便觀察
        
        if decision['action'] == 'srs_review':
            st.session_state.due_cards = fetch_srs_words(mode="review", limit=15)
            st.toast("🎯 為您準備了今天的複習清單！")
        
        elif decision['action'] == 'learn_new':
            st.session_state.due_cards = fetch_srs_words(mode="new", limit=15)
            st.toast("✨ 為您抓取了一批全新單字！")
       
        elif decision['action'] in ['search_example', 'search_news', 'search_word']: 
            mode_map = {
                "search_example": "example", 
                "search_news": "news", 
                "search_word": "word"
            }
            mode = mode_map[decision['action']]
            
            st.session_state.due_cards = semantic_search(
                decision['query'], 
                mode=mode, 
                srs_filter=decision['srs_filter']
            )
            
            if st.session_state.due_cards:
                st.toast(f"🔍 找到了 {len(st.session_state.due_cards)} 個符合條件的單字！")
            else:
                st.warning("查無符合所有條件的單字 (可能該情境下沒有您指定的新/舊字了)。")
        
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
        st.session_state.due_cards = fetch_srs_words(mode="review", limit=15)
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
    flip_placeholder = st.empty()
    
    if not st.session_state.card_flipped:
        with flip_placeholder.container():
            if st.button("👁️ 翻開記憶卡看答案", key=f"flip_{target_word}_{current_num}", use_container_width=True):
                st.session_state.card_flipped = True
                st.rerun()
            
    # 3. 翻牌後的單字卡內容
    else:
        flip_placeholder.empty() # 翻牌後強制炸毀包廂，確保絕對沒有殘影！
        
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
            if st.button("😭 忘記了", key=f"btn0_{target_word}_{current_num}", use_container_width=True): handle_feedback(0); st.rerun()
        with col2:
            if st.button("😓 很吃力", key=f"btn1_{target_word}_{current_num}", use_container_width=True): handle_feedback(1); st.rerun()
        with col3:
            if st.button("🙂 背起來", key=f"btn2_{target_word}_{current_num}", use_container_width=True): handle_feedback(4); st.rerun()
        with col4:
            if st.button("😎 太簡單", key=f"btn3_{target_word}_{current_num}", use_container_width=True): handle_feedback(5); st.rerun()