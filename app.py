import streamlit as st
from supabase import create_client, Client
from datetime import datetime, timezone, timedelta
from gtts import gTTS
import io
import re

# ==========================================
# 1. 系統初始化與雲端連線
# ==========================================
st.set_page_config(page_title="NOVA | Agentic 時事單字庫", page_icon="🌍", layout="centered")

@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_supabase()

# ==========================================
# 2. 資料庫操作與語音函數
# ==========================================
def fetch_due_cards():
    cache_res = supabase.table("llm_generation_cache").select("*").execute()
    all_cards = {card['word']: card for card in cache_res.data}
    
    progress_res = supabase.table("user_srs_progress").select("*").execute()
    user_progress = {p['word']: p for p in progress_res.data}
    
    due_cards = []
    today = datetime.now(timezone.utc).date()
    
    for word, card_data in all_cards.items():
        if word in user_progress:
            due_date = datetime.strptime(user_progress[word]['due_date'], "%Y-%m-%d").date()
            if due_date <= today:
                card_data['srs'] = user_progress[word]
                due_cards.append(card_data)
        else:
            card_data['srs'] = None
            due_cards.append(card_data)
    return due_cards

def update_srs_progress(word, quality, current_srs):
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
    """將文字轉換為語音，並直接在 Streamlit 顯示播放器"""
    tts = gTTS(text=text, lang='en', slow=False)
    audio_bytes = io.BytesIO()
    tts.write_to_fp(audio_bytes)
    st.audio(audio_bytes.getvalue(), format='audio/mp3')

# ==========================================
# 3. Streamlit 前端 UI 介面
# ==========================================
st.title("🌍 NOVA 智能時事單字庫")
st.markdown("*Agentic News-Driven Vocabulary Builder (ANDVB)*")

with st.sidebar:
    st.header("⚙️ 系統控制")
    
    # 顯示目前記憶體裡到底抓到了幾題，方便我們除錯
    current_count = len(st.session_state.get('due_cards', []))
    st.caption(f"📌 目前載入任務數：{current_count} 題")
    
    if st.button("🔄 強制同步雲端單字", use_container_width=True):
        # 1. 第一步：徹底炸毀 Streamlit 的快取記憶！
        st.cache_data.clear()
        
        # 2. 第二步：重新去 Supabase 撈取最熱騰騰的資料
        st.session_state.due_cards = fetch_due_cards()
        st.session_state.current_index = 0
        st.session_state.card_flipped = False
        
        # 3. 第三步：重新載入畫面
        st.rerun()

if 'due_cards' not in st.session_state:
    with st.spinner("☁️ 正在從雲端金庫讀取今日教材..."):
        st.session_state.due_cards = fetch_due_cards()
if 'current_index' not in st.session_state:
    st.session_state.current_index = 0
if 'card_flipped' not in st.session_state:
    st.session_state.card_flipped = False

if st.session_state.current_index >= len(st.session_state.due_cards):
    st.balloons()
    st.success("🎉 太棒了！你已經清空了今天的複習任務！明天再來吧！")
    if st.button("🔄 重新檢查雲端更新"):
        st.session_state.due_cards = fetch_due_cards()
        st.session_state.current_index = 0
        st.rerun()
else:
    total_cards = len(st.session_state.due_cards)
    current_num = st.session_state.current_index + 1
    st.progress(current_num / total_cards, text=f"今日進度：{current_num} / {total_cards}")
    
    current_card = st.session_state.due_cards[st.session_state.current_index]
    target_word = current_card['word']
    
    st.markdown("### 📰 閱讀新聞，猜猜這是什麼字？")
    
    # 尋找 "📌 **焦點詞彙**：**片語**" 中間的那串字
    match = re.search(r"📌 \*\*焦點詞彙\*\*：\*\*(.*?)\*\*", current_card['teacher_card_content'])
    
    # 如果有找到片語就用片語，沒找到就退回使用原始單字
    actual_focus_phrase = match.group(1).strip() if match else target_word
    context_masked = re.sub(re.escape(actual_focus_phrase), " **[_____]** ", current_card['news_context'], flags=re.IGNORECASE)
    st.info(context_masked)
    
    if not st.session_state.card_flipped:
        if st.button("👁️ 翻開記憶卡看答案", use_container_width=True):
            st.session_state.card_flipped = True
            st.rerun()
            
    if st.session_state.card_flipped:
        st.divider()
        content = current_card['teacher_card_content']
        
        # 1. 強化版解析函式 (增加容錯)
        def get_part(text, start, end=None):
            if start not in text: return ""
            s = text.find(start) + len(start)
            if end and end in text[s:]:
                e = text.find(end, s)
                return text[s:e].strip()
            return text[s:].strip()

        # --- 第一區塊：新聞原句 (只留原文、聲音、翻譯) ---
        st.markdown("### 📖 時事單字記憶卡")
        
        # 抓取原文：從「新聞原句」到「中文翻譯」
        news_en = get_part(content, "📰 **新聞原句**：", "📰 **中文翻譯**：").strip().strip('"')
        # 抓取翻譯：從「中文翻譯」到「單字與詞性」 (確保對齊你的截圖標籤)
        news_zh = get_part(content, "📰 **中文翻譯**：", "📌")
        
        st.markdown("##### 📰 新聞原句 News Context")
        st.write(news_en)
        text_to_speech_player(news_en)
        if news_zh:
            st.write(news_zh)
        
        st.divider()

        # --- 第二區塊：焦點詞彙 (只留詞彙資訊) ---
        st.markdown("### 📌 焦點詞彙與解釋")
        # 抓取詞彙：從「單字與詞性」到「生活例句」
        vocab_info = get_part(content, "📌", "💡")
        if vocab_info:
            st.info(f"**{target_word}**\n\n{vocab_info}")
        
        st.divider()

        # --- 第三區塊：生活例句 (左右切分，包含聲音) ---
        st.markdown("### 💡 生活情境造句 Daily Life Example")
        
        example_en = get_part(content, "**🇺🇸**：", "**🇹🇼**：")
        example_zh = get_part(content, "**🇹🇼**：")
        
        col_en, col_zh = st.columns(2)
        with col_en:
            st.markdown("##### 🇺🇸 English")
            st.info(example_en)
            text_to_speech_player(example_en)
            
        with col_zh:
            st.markdown("##### 🇹🇼 中文翻譯")
            st.success(example_zh)

        # --- 4. 測驗題區塊 ---
        st.divider()
        with st.expander("🕵️‍♂️ [選作] 點我挑戰魔王考官測驗題"):
            st.markdown(current_card['examiner_quiz_content'])
        
        col1, col2, col3, col4 = st.columns(4)
        
        def handle_feedback(quality):
            update_srs_progress(target_word, quality, current_card['srs'])
            st.session_state.current_index += 1
            st.session_state.card_flipped = False
            
        with col1:
            if st.button("😭 忘記了", use_container_width=True): handle_feedback(0); st.rerun()
        with col2:
            if st.button("😓 很吃力", use_container_width=True): handle_feedback(1); st.rerun()
        with col3:
            if st.button("🙂 背起來", use_container_width=True): handle_feedback(4); st.rerun()
        with col4:
            if st.button("😎 太簡單", use_container_width=True): handle_feedback(5); st.rerun()