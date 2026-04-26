import re
import os
import json
import feedparser
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
import random

# LangChain & Groq 相關
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, START, END

# Supabase 相關
from supabase import create_client, Client

print("=========================================")
print("🚀 NOVA SYSTEM BOOTING: VERSION MVC-4.0")
print("=========================================")

# ==========================================
# 1. 環境設定與初始化
# ==========================================
# 載入 .env 檔案中的變數
load_dotenv()

# 初始化 Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 初始化 Groq LLM (確認 .env 中有 GROQ_API_KEY)
# 注意：替換為實際使用的模型名稱
# qwen/qwen3-32b
# llama-3.3-70b-versatile
# llama-3.1-8b-instant
MODEL_NAME = "llama-3.3-70b-versatile" 

# ==========================================
# 2. Supabase 管理員類別
# ==========================================
class SupabaseManager:
    @staticmethod
    def get_word_record(word: str):
        response = supabase.table("llm_generation_cache").select("word, updated_at, update_count").eq("word", word.lower()).execute()
        return response.data[0] if response.data else None

    @staticmethod
    # 🌟 參數新增了 raw_example_en 與 raw_quiz_en
    def update_generation_result(word, context, teacher_card, quiz, current_count, raw_example_en="", raw_quiz_en=""):
        data = {
            "news_context": context,
            "teacher_card_content": teacher_card,
            "examiner_quiz_content": quiz,
            "raw_example_en": raw_example_en,
            "raw_quiz_en": raw_quiz_en,
            "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "update_count": current_count + 1
        }
        supabase.table("llm_generation_cache").update(data).eq("word", word.lower()).execute()

    @staticmethod
    def save_new_generation(word, context, teacher_card, quiz, raw_example_en="", raw_quiz_en=""):
        # 1. 存入教材緩存表
        data = {
            "word": word.lower(),
            "news_context": context,
            "teacher_card_content": teacher_card,
            "examiner_quiz_content": quiz,
            "raw_example_en": raw_example_en,
            "raw_quiz_en": raw_quiz_en,
            "update_count": 0
        }
        supabase.table("llm_generation_cache").insert(data).execute()

        # 2. 同步初始化 SRS 進度 (讓單字進入遺忘曲線)
        srs_data = {
            "word": word.lower(),
            "easiness_factor": 2.5,        # 初始容易度
            "interval": 0,                 # 初始間隔
            "repetition_count": 0,         # 尚未開始複習
            "next_review_date": datetime.now(timezone.utc).replace(microsecond=0).isoformat() 
        }
        try:
            supabase.table("user_srs_progress").insert(srs_data).execute()
            print(f"   📈 SRS 初始化完成：'{word}' 已加入複習排程。")
        except Exception as e:
            print(f"   ⚠️ SRS 初始化失敗 (可能已存在)：{e}")

    @staticmethod
    def get_today_added_count():
        """查詢今天（UTC時間）已經新增了多少個單字"""
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        
        # 查詢 llm_generation_cache 中，created_at 大於等於今天凌晨的資料
        # 注意：Supabase 表格預設會有 created_at 欄位
        response = supabase.table("llm_generation_cache") \
            .select("word", count="exact") \
            .gte("created_at", today_start) \
            .execute()
        
        return response.count if response.count is not None else 0

# ==========================================
# 3. 爬蟲與字典交集 (ETL 引擎)
# ==========================================
def fetch_diverse_learning_materials():
    print("🌍 啟動 NOVA 終極爬蟲引擎（隨機 3 領域 x 15 筆 = 45 候選）...")
    
    # 讀取本地字典
    df_vocab = pd.read_csv("data/vocab_advanced_clean.csv")
    advanced_words_set = set(df_vocab['word'].str.lower())
    
    # 總頻道池 (Master Pool)
    MASTER_FEEDS = {
        "World": "http://feeds.bbci.co.uk/news/world/rss.xml",
        "Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
        "Tech": "http://feeds.bbci.co.uk/news/technology/rss.xml",
        "Science": "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "Health": "http://feeds.bbci.co.uk/news/health/rss.xml",
        "Education": "http://feeds.bbci.co.uk/news/education/rss.xml"
    }
    
    # 🎲 第一層隨機：每天從總池子裡「隨機盲抽 3 個領域」
    selected_categories = random.sample(list(MASTER_FEEDS.keys()), 3)
    print(f"🎲 今日幸運領域：{', '.join(selected_categories)}\n")
    
    full_candidate_pool = []
    
    # 🎲 第二層分層：針對抽中的這 3 個領域，各抓 15 筆
    for category in selected_categories:
        url = MASTER_FEEDS[category]
        print(f"📡 正在抓取 [{category}] 領域...")
        feed = feedparser.parse(url)
        
        count = 0
        for entry in feed.entries:
            if count >= 15: break # 每個頻道嚴格把關只拿 15 筆
            
            summary_text = BeautifulSoup(entry.summary, "html.parser").get_text()
            words_in_news = set(summary_text.lower().replace('.', ' ').replace(',', ' ').split())
            
            # 尋找難字交集
            matched_words = words_in_news.intersection(advanced_words_set)
            
            if matched_words:
                target_word = max(matched_words, key=len)
                full_candidate_pool.append({
                    "Target_Word": target_word,
                    "News_Context": summary_text,
                    "Category": category # 標註領域，方便日誌觀察
                })
                advanced_words_set.discard(target_word) # 防止同一次執行中重複挑字
                count += 1
                
    # 🎲 第三層隨機：全局大洗牌 (Global Shuffle)
    random.shuffle(full_candidate_pool)
    print(f"✅ 完成！共建立 {len(full_candidate_pool)} 筆跨領域候選池。")
    
    return full_candidate_pool

# ==========================================
# 4. 定義 AI 員工 (LangGraph LCEL 節點)
# ==========================================
llm_assessor = ChatGroq(model=MODEL_NAME, temperature=0.1).bind(response_format={"type": "json_object"})
llm_teacher = ChatGroq(model=MODEL_NAME, temperature=0.2).bind(response_format={"type": "json_object"})
llm_examiner = ChatGroq(model=MODEL_NAME, temperature=0.4).bind(response_format={"type": "json_object"})
llm_reviewer = ChatGroq(model=MODEL_NAME, temperature=0.1).bind(response_format={"type": "json_object"})
parser = JsonOutputParser()

def assessor_node(state):
    print(f"   ⚖️ [評估中] 判斷 '{state['current_word']}' 是否達標...")
    prompt = ChatPromptTemplate.from_messages([
        ("system", '你是極度嚴格的單字難度評估員，只能輸出 JSON。輸出格式請嚴格遵守：{{ "is_suitable": false, "reason": "太簡單" }}'),
        ("user", "判斷單字 '{word}' 是否達 TOEIC 850 / CEFR C1 進階難度。如果是太簡單的國中、高中基礎單字 (如 empty, there, monday, first 等) 請一律給 false。")
    ])
    res = (prompt | llm_assessor | parser).invoke({"word": state['current_word']})
    
    # 確保 LLM 回傳的是真正的 Boolean
    is_suitable = res.get("is_suitable", True)
    if str(is_suitable).lower() == 'false':
        is_suitable = False
        
    print(f"   ↳ 評估結果: {is_suitable} (理由: {res.get('reason', '無')})")
    return {"is_suitable": is_suitable}

def teacher_node(state):
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是專業嚴謹的英文老師，只能輸出 JSON。"),
        # 加入「語塊 (Lexical Chunk)」感知指令
        ("user", """
        請為單字 '{word}' 在原句 '{context}' 中製作記憶卡。
        【核心規則】：
        請檢視原句，如果 '{word}' 是某個「片語」或「固定搭配詞」
        (例如 set up, consist of, take for granted) 的一部分，
        自動將「整個片語」當作本次的教學主體！
         
        【生活例句生成鐵則】：
        你的 'example_sentence_en' 必須是「超級接地氣的日常對話或生活情境」，並且 **絕對禁止** 與新聞原句 '{context}' 的主題重疊！
        - 如果新聞是「政治/財經/科技/戰爭」，你的例句就必須強制切換到：「辦公室八卦、情侶日常、點餐購物、旅遊迷路、朋友閒聊」等充滿人味的情境。
        - 句子要有畫面感，不要寫像教科書一樣死板的句子。
        
        
        輸出 JSON 需包含以下 key：
        word (請填入該單字或完整片語),
        part_of_speech (詞性或標註為 phrase),
        kk_phonetics (音標，片語可省略),
        chinese_meaning (解釋),
        news_translation (整句新聞的流暢中文翻譯),
        example_sentence_en (生活例句，必須包含該單字或片語，超級接地氣、完全脫離新聞主題的純英文生活例句),
        example_sentence_zh (生活例句的台灣慣用語氣翻譯)
        """)
    ])
    # 傳入兩個變數給 LangChain
    data = (prompt | llm_teacher | parser).invoke({
        "word": state['current_word'], 
        "context": state['news_context']
    })
    
    return {"raw_teacher_data": data}

def examiner_node(state):
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是嚴格的考官，只能輸出 JSON。"),
        ("user", """
        請針對焦點詞彙 '{word}' 設計一題四選一的英文填空題。
        
        【核心規則 (必須遵守不得違反!題目及選項必須使用英文撰寫)】：
        1. 語言隔離：'question' (題目) 與 'options' (四個選項) 必須是 **100% totally in English**，絕對不允許出現任何中文字！
        2. 挖空規則：如果 '{word}' 在原句中屬於片語 (例如 look forward to)，請在題目中「將整個片語挖空 (用 _____ 取代)」，絕對不要只挖空一半！
        3. 選項對稱：選項 (A, B, C, D) 的長度、時態與結構必須一致。
        
        輸出 JSON 需包含：
        question (帶有 _____ 的純英文題目),
        options (包含 A, B, C, D 四個 key 的純英文物件),
        answer (正確選項字母),
        translation (題目繁體中文翻譯),
        explanation (繁體中文解析，需說明為何選此答案以及其他選項為何錯誤)
        """)
    ])
    data = (prompt | llm_examiner | parser).invoke({"word": state['current_word']})

    return {"raw_quiz_data": data}

def reviewer_node(state):
    print(f"   🔍 [QA總編輯品管中] 正在檢查與優化 '{state['current_word']}'...")
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是嚴格的教材總編輯兼 QA 品管員。只能輸出純 JSON 格式。"),
        ("user", """
        請檢視並優化以下兩組 JSON 資料，並萃取純淨的英文原句供系統做向量檢索。
        
        【🛡️ QA 品管任務】：
        1. 檢查所有焦點詞彙是否提取正確，確實為 '{word}' ，若不正確則需要重新生成資料。
        2. 檢查老師資料的 'example_sentence_en'：**必須是全英文**。
        3. 檢查考官資料的 'question' 與 'options'：**必須是全英文** 如果裡面不小心混入了「中文」，請你立刻改寫為「全英文」。
        4. 考官資料的 'question' 必須將焦點單字 '{word}' 挖空 (用 _____ 取代)，不可提前洩題。
        5. 優化中文：將所有的中文欄位潤飾成通順的台灣慣用語法並加上標點符號。
        
        【待潤飾老師資料】：
        {teacher_data}
        
        【待潤飾考官資料】：
        {quiz_data}
        
        請輸出 JSON，必須嚴格包含以下 4 個 Key：
        {{
            "polished_teacher": {{ ...保持原本 key 結構的優化後老師資料... }},
            "polished_quiz": {{ ...保持原本 key 結構的優化後考官資料... }},
            "raw_example_en": "從老師資料中提取的 example_sentence_en (純英文，不可有中文與 Markdown)",
            "raw_quiz_en": "請將考官資料的 question 裡的 _____ 替換成正確單字 '{word}' 後的「完整純英文原句」(不可有底線與 Markdown)"
        }}
        """)
    ])
    
    try:
        raw_res = (prompt | llm_reviewer | parser).invoke({
            "teacher_data": state.get('raw_teacher_data', {}),
            "quiz_data": state.get('raw_quiz_data', {}),
            "word": state['current_word']
        })
        final_teacher = raw_res.get('polished_teacher') or state.get('raw_teacher_data', {})
        final_quiz = raw_res.get('polished_quiz') or state.get('raw_quiz_data', {})
        
        raw_example = raw_res.get('raw_example_en')
        if not raw_example:
            raw_example = final_teacher.get('example_sentence_en', '')
            
        raw_quiz = raw_res.get('raw_quiz_en')
        if not raw_quiz:
            # 自己把測驗題的底線換回原本的單字
            q_text = final_quiz.get('question', '')
            raw_quiz = q_text.replace('_____', state['current_word']).replace('[_____]', state['current_word'])
            
        raw_example = str(raw_example).strip() if raw_example else ""
        raw_quiz = str(raw_quiz).strip() if raw_quiz else ""
        
        print(f"   ✅ 數據萃取成功：Example=[{raw_example[:30]}...], Quiz=[{raw_quiz[:30]}...]")
        
    except Exception as e:
        print(f"   ⚠️ [警告] 總編輯罷工，啟用備用原始資料！錯誤: {e}")
        final_teacher = state.get('raw_teacher_data', {})
        final_quiz = state.get('raw_quiz_data', {})
        raw_example = str(final_teacher.get('example_sentence_en', '')).strip()
        
        # 降級時也自己填補底線
        q_text = final_quiz.get('question', '')
        raw_quiz = str(q_text.replace('_____', state['current_word']).replace('[_____]', state['current_word'])).strip()
        print(f"   ⚠️ 啟動備用數據：Example=[{raw_example[:30]}...], Quiz=[{raw_quiz[:30]}...]")
    
    # 組合卡片內容
    card = f"""[📖 時事單字記憶卡]
📰 **新聞原句**："{state['news_context']}"
📰 **中文翻譯**：{final_teacher.get('news_translation', '')}

📌 **焦點詞彙**：**{final_teacher.get('word', '')}** ({final_teacher.get('part_of_speech', '')}) {final_teacher.get('kk_phonetics', '')}
📖 **解釋**：{final_teacher.get('chinese_meaning', '')}

💡 **生活例句**：
**🇺🇸**：{final_teacher.get('example_sentence_en', '')}
**🇹🇼**：{final_teacher.get('example_sentence_zh', '')}
"""

    options = final_quiz.get('options', {})
    quiz = f"""[💡 情境測驗題]
{final_quiz.get('question', '')}

(A) {options.get('A','')}  (B) {options.get('B','')}  
(C) {options.get('C','')}  (D) {options.get('D','')}

[正確解答] {final_quiz.get('answer', '')}
[情境翻譯] {final_quiz.get('translation', '')}
[解析] {final_quiz.get('explanation', '')}
"""

    return {
        "teacher_card": card,          
        "quiz": quiz,                  
        "raw_example_en": raw_example,
        "raw_quiz_en": raw_quiz
    }

# 建立 State 與 Graph
class AgentState(TypedDict):
    current_word: str
    news_context: str
    is_suitable: Optional[bool]
    raw_teacher_data: Optional[dict]
    raw_quiz_data: Optional[dict]
    teacher_card: Optional[str]
    quiz: Optional[str]
    raw_example_en: Optional[str]
    raw_quiz_en: Optional[str]

# BACKEND WORKFLOW
workflow = StateGraph(AgentState)
workflow.add_node("Assessor", assessor_node)
workflow.add_node("Teacher", teacher_node)
workflow.add_node("Examiner", examiner_node)
workflow.add_node("Reviewer", reviewer_node)

workflow.add_edge(START, "Assessor")
workflow.add_conditional_edges("Assessor", lambda s: "Teacher" if s.get("is_suitable") else END)
workflow.add_edge("Teacher", "Examiner")
workflow.add_edge("Examiner", "Reviewer")
workflow.add_edge("Reviewer", END)
app = workflow.compile()

# ==========================================
# 5. 雲端量產工廠
# ==========================================
def mass_produce_flashcards_with_refresh(candidates, target_daily_count=5):
    """
    負責接收提煉好的單字 List，直到「成功」產出指定數量後才停止。
    """
    if not candidates:
        print("今天沒有抓到合適的單字候選名單。")
        return

    print(f"🔄 啟動 [時效性感知] 雲端量產工廠 (目標：成功補齊 {target_daily_count} 個新教材)...\n")
    
    success_count = 0 
    
    for attempts, item in enumerate(candidates, 1):
        
        if success_count >= target_daily_count:
            print(f"\n🎯 報告老闆！今日產線任務達標，已成功備妥 {target_daily_count} 個單字，順利停機！")
            break
            
        target_word = item['Target_Word'].lower()
        context = item['News_Context']
        category = item.get('Category', 'Unknown') 
        
        print("-" * 50)
        print(f"⏳ [產能 {success_count}/{target_daily_count} | 消耗候選 {attempts}/{len(candidates)}]")
        print(f"📡 領域: [{category}] | 🎯 測試單字: '{target_word}'")
        
        record = SupabaseManager.get_word_record(target_word)
        should_generate, is_update, current_count = False, False, 0

        if record:
            last_updated = datetime.fromisoformat(record['updated_at'].replace('Z', '+00:00'))
            days_diff = (datetime.now(timezone.utc) - last_updated).days
            current_count = record.get('update_count', 0)

            if days_diff >= 15:
                print(f"   ♻️ '{target_word}' 已過期 ({days_diff} 天未見)，準備更新...")
                should_generate = True
                is_update = True
            else:
                print(f"   ⏭️ '{target_word}' 剛更新過，跳過。")
                continue 
        else:
            print(f"   ✨ 發現全新單字，開始呼叫 AI 產製...")
            should_generate = True

        if should_generate:
            try:
                final_state = app.invoke({"current_word": target_word, "news_context": context})
                
                if not final_state.get("is_suitable", False):
                    print(f"   🛑 淘汰 (難度不符或格式錯誤)，尋找下一個。")
                    continue 
                
                # 📦 1. 取得排版好的卡片
                teacher_card = final_state.get('teacher_card', '')
                quiz = final_state.get('quiz', '')
                
                # 🛡️ 2. 終極防線：獲取純淨句子。
                raw_example = final_state.get('raw_example_en')
                # 攔截空值、以及被誤轉為 "None" 的字串
                if not raw_example or str(raw_example).strip() == "None" or str(raw_example).strip() == "":
                    print("   🔴 [嚴重錯誤] raw_example 遺失，嘗試從原始資料手動補救...")
                    raw_teacher = final_state.get('raw_teacher_data', {})
                    raw_example = raw_teacher.get('example_sentence_en', '')

                raw_quiz = final_state.get('raw_quiz_en')
                if not raw_quiz or str(raw_quiz).strip() == "None" or str(raw_quiz).strip() == "":
                    print("   🔴 [嚴重錯誤] raw_quiz 遺失，嘗試從原始資料手動補救...")
                    raw_exam_data = final_state.get('raw_quiz_data', {})
                    q_text = raw_exam_data.get('question', '')
                    raw_quiz = q_text.replace('_____', target_word).replace('[_____]', target_word)

                # 🛡️ 3. 強制轉為字串，給予明確錯誤字眼，避免資料庫顯示 NULL
                raw_example = str(raw_example).strip() if raw_example else "ERROR_EMPTY_EXAMPLE"
                raw_quiz = str(raw_quiz).strip() if raw_quiz else "ERROR_EMPTY_QUIZ"
                
                # 📊 入庫前最後監視器：印出準備存入的句子片段
                print(f"   📥 [準備寫入DB] Example: [{raw_example[:30]}...] | Quiz: [{raw_quiz[:30]}...]")
                
                if is_update:
                    SupabaseManager.update_generation_result(
                        target_word, context, teacher_card, quiz, current_count, raw_example, raw_quiz
                    )
                    print(f"   ✅ '{target_word}' 雲端更新完成！")
                else:
                    SupabaseManager.save_new_generation(
                        target_word, context, teacher_card, quiz, raw_example, raw_quiz
                    )
                    print(f"   ✅ '{target_word}' 已成功存入雲端！")
                    
                success_count += 1 
                
            except Exception as e:
                print(f"   ❌ 處理 '{target_word}' 發生錯誤: {e}")

# ==========================================
# 6. 掃地機器人 (更新Embedding)
# ==========================================
def sync_missing_embeddings():
    """
    🧹 更新補上Embedding：自動掃描並對應 raw_example_en 與 raw_quiz_en 進行向量編碼
    """
    print("\n🔍 [自癒機制] 正在檢查是否有單字需要注入語意大腦...")
    
    response = supabase.table("llm_generation_cache") \
        .select("*") \
        .is_("example_embedding", "null") \
        .execute()
        
    records = response.data
    
    if not records:
        print("   ✅ 檢查完畢：目前資料庫中所有單字與情境皆具備完整向量。")
        return

    print(f"   ⚠️ 發現 {len(records)} 筆資料需要編碼。正在載入 HuggingFace 模型 (all-MiniLM-L6-v2)...")
    
    from langchain_community.embeddings import HuggingFaceEmbeddings
    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    
    for record in records:
        word = record['word']
        context = record.get('news_context', '')
        
        raw_ex = record.get('raw_example_en') or ''
        raw_qz = record.get('raw_quiz_en') or ''
        
        combined_situational_text = f"{raw_ex}\n{raw_qz}".strip()
        
        try:
            word_vec = embeddings.embed_query(word)
            context_vec = embeddings.embed_query(context) if context else None
            
            if combined_situational_text and combined_situational_text != "None\nNone":
                example_vec = embeddings.embed_query(combined_situational_text)
            else:
                example_vec = None
            
            update_payload = {
                "word_embedding": word_vec,
                "example_embedding": example_vec
            }
            if context_vec: 
                update_payload["context_embedding"] = context_vec
            
            supabase.table("llm_generation_cache").update(update_payload).eq("word", word).execute()
            print(f"   ✅ '{word}' 的 3D 語意大腦已同步完成！")
            
        except Exception as e:
            print(f"   ❌ '{word}' 編碼失敗: {e}")

    print("✨ 所有資料已成功補齊向量大腦，現在 RAG 搜尋會精準到不行！")
# ==========================================
# 7. 主排程器 (Main Execution)
# ==========================================
def main():
    print("🚀 系統啟動：開始執行 NOVA 每日採集排程...\n")
    
    try:
        DAILY_QUOTA = int(os.getenv("TARGET_DAILY_COUNT", 3))
        print(f"⚙️ 系統設定：每日目標配額為 {DAILY_QUOTA} 筆。")
    except ValueError:
        DAILY_QUOTA = 5
        print(f"⚠️ 警告：環境變數格式錯誤，回歸預設值 {DAILY_QUOTA} 筆。")
    
    # 檢查今天已經用掉了多少配額
    already_added = SupabaseManager.get_today_added_count()
    remaining_quota = DAILY_QUOTA - already_added
    
    try:
        if remaining_quota <= 0:
            print(f"🛑 今日配額已滿 ({already_added}/{DAILY_QUOTA})。跳過新單字抓取階段。")
        else:
            print(f"📊 今日狀態：已完成 {already_added} 個，尚需補充 {remaining_quota} 個。")
            candidates = fetch_diverse_learning_materials()
            
            # 執行產線，填滿剩餘配額
            mass_produce_flashcards_with_refresh(candidates, target_daily_count=remaining_quota)
            
    except Exception as e:
        # 如果中途遇到 LLM 罷工、新聞網站連不上等意外，系統不會整個死掉
        print(f"\n❌ [系統異常] 產線執行過程中發生錯誤: {e}")
        
    finally:
        print("\n=============================================")
        sync_missing_embeddings()  # 智能掃地機器人出動！
        print("=============================================")
        print("\n🎉 NOVA 每日採集與自我修復排程執行完畢！")


if __name__ == "__main__":
    main()