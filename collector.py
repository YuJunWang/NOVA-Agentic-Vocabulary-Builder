import re
import os
import json
import feedparser
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

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
    def update_generation_result(word, context, teacher_card, quiz, current_count):
        data = {
            "news_context": context,
            "teacher_card_content": teacher_card,
            "examiner_quiz_content": quiz,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "update_count": current_count + 1
        }
        supabase.table("llm_generation_cache").update(data).eq("word", word.lower()).execute()

    @staticmethod
    def save_new_generation(word, context, teacher_card, quiz):
        data = {
            "word": word.lower(),
            "news_context": context,
            "teacher_card_content": teacher_card,
            "examiner_quiz_content": quiz,
            "update_count": 0
        }
        supabase.table("llm_generation_cache").insert(data).execute()
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
def fetch_and_extract_daily_vocab():
    print("🌍 啟動 BBC RSS 爬蟲與單字提煉引擎...")
    
    # 讀取本地字典檔
    df_vocab = pd.read_csv("data/vocab_advanced_clean.csv")
    advanced_words_set = set(df_vocab['word'].str.lower())
    
    feed_url = "http://feeds.bbci.co.uk/news/world/rss.xml"
    feed = feedparser.parse(feed_url)
    
    learning_materials = []
    
    for entry in feed.entries[:20]: # 取前 20 篇新聞測試
        summary_text = BeautifulSoup(entry.summary, "html.parser").get_text()
        words_in_news = set(summary_text.lower().replace('.', '').replace(',', '').split())
        
        # 找交集
        matched_words = words_in_news.intersection(advanced_words_set)
        
        if matched_words:
            target_word = max(matched_words, key=len)
            learning_materials.append({
                "Target_Word": target_word,
                "News_Context": summary_text,
                "Article_Title": entry.title
            })
            advanced_words_set.remove(target_word) # 防止重複挑字
            
    return pd.DataFrame(learning_materials)

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
        ("user", "判斷單字 '{word}' 是否達 TOEIC 850 / CEFR C1 進階難度。如果是國中、高中基礎單字 (如 empty, there, monday, first 等) 請一律給 false。")
    ])
    res = (prompt | llm_assessor | parser).invoke({"word": state['current_word']})
    
    # 防呆機制：確保 LLM 回傳的是真正的 Boolean
    is_suitable = res.get("is_suitable", True)
    if str(is_suitable).lower() == 'false':
        is_suitable = False
        
    print(f"   ↳ 評估結果: {is_suitable} (理由: {res.get('reason', '無')})")
    return {"is_suitable": is_suitable}

def teacher_node(state):
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是專業的英文老師，只能輸出 JSON。"),
        # 加入「語塊 (Lexical Chunk)」感知指令
        ("user", """
        請為單字 '{word}' 在原句 '{context}' 中製作記憶卡。
        【⚠️ 核心規則】：請檢視原句，如果 '{word}' 是某個「片語」或「固定搭配詞」(例如 set up, consist of, take for granted) 的一部分，請自動將「整個片語」當作本次的教學主體！
        
        輸出 JSON 需包含以下 key：
        word (請填入該單字或完整片語),
        part_of_speech (詞性或標註為 phrase),
        kk_phonetics (音標，片語可省略),
        chinese_meaning (解釋),
        news_translation (整句新聞的流暢中文翻譯),
        example_sentence_en (生活例句，必須包含該單字或片語),
        example_sentence_zh (例句翻譯)
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
        【⚠️ 核心規則】：
        1. 如果 '{word}' 在原句中屬於片語 (例如 look forward to)，請在題目中「將整個片語挖空 (用 _____ 取代)」，絕對不要只挖空一半！
        2. 選項 (A, B, C, D) 的長度與結構必須一致 (例如正確答案是片語，干擾選項也必須是語意不通的片語)。
        
        輸出 JSON 需包含：
        question (帶有 _____ 的全英文敘述題目),
        options (包含 A, B, C, D 四個 key 的物件),
        answer (正確選項字母),
        translation (題目的通順中文翻譯),
        explanation (解析，需說明為何選此答案以及其他選項為何錯誤)
        """)
    ])
    data = (prompt | llm_examiner | parser).invoke({"word": state['current_word']})

    return {"raw_quiz_data": data}

def reviewer_node(state):
    print(f"   🔍 [QA總編輯潤飾中] 正在優化 '{state['current_word']}'...")
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是專業教材總編輯。你的任務是優化 JSON 資料裡的「中文翻譯」與「解釋流暢度」，確保符合台灣慣用語。"),
        ("user", """
        請檢視並優化以下兩組 JSON 資料的中文內容 (news_translation, chinese_meaning, example_sentence_zh, translation, explanation)。
        【⚠️ 核心規則】：請保留原本所有的 JSON Key 不變，只優化 Value 的中文內容。
        
        【待潤飾老師資料】：
        {teacher_data}
        
        【待潤飾考官資料】：
        {quiz_data}
        
        請輸出 JSON，必須包含兩個 Key：'polished_teacher' 與 'polished_quiz'，裡面分別包裝優化後的完整字典。
        """)
    ])
    
    # 總編輯拿到純資料，改完後吐出純資料
    try:
        raw_res = (prompt | llm_reviewer | parser).invoke({
            "teacher_data": state.get('raw_teacher_data', {}),
            "quiz_data": state.get('raw_quiz_data', {})
        })
        final_teacher = raw_res.get('polished_teacher') or state.get('raw_teacher_data', {})
        final_quiz = raw_res.get('polished_quiz') or state.get('raw_quiz_data', {})
    except Exception as e:
        print(f"   ⚠️ [警告] 總編輯罷工或 JSON 壞掉，啟用備用原始資料！錯誤: {e}")
        final_teacher = state.get('raw_teacher_data', {})
        final_quiz = state.get('raw_quiz_data', {})
    

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

    # 最終輸出完美不跑版的字串給後續存檔
    return {
        "teacher_card": card,
        "quiz": quiz
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
def mass_produce_flashcards_with_refresh(df_news_material, target_daily_count=3):
    """
    負責接收提煉好的單字 DataFrame，直到「成功」產出指定數量後才停止。
    """
    if df_news_material.empty:
        print("今天沒有抓到合適的單字。")
        return

    print(f"🔄 啟動 [時效性感知] 雲端量產工廠 (目標：成功產出 {target_daily_count} 個新教材)...\n")
    
    success_count = 0 
    
    for _, row in df_news_material.iterrows():
        
        if success_count >= target_daily_count:
            print(f"\n🎯 報告老闆！已成功為您備妥 {target_daily_count} 個單字，今日產線順利停機！")
            break
            
        target_word = row['Target_Word'].lower()
        context = row['News_Context']
        
        record = SupabaseManager.get_word_record(target_word)
        should_generate, is_update, current_count = False, False, 0

        if record:
            # 處理已存在的單字，檢查是否需要更新
            last_updated = datetime.fromisoformat(record['updated_at'].replace('Z', '+00:00'))
            days_diff = (datetime.now(timezone.utc) - last_updated).days
            current_count = record.get('update_count', 0)

            if days_diff >= 15:
                print(f"♻️ '{target_word}' 已過期，準備更新...")
                should_generate = True
                is_update = True
            else:
                print(f"⏭️ '{target_word}' 剛更新過，跳過。")
                continue # 這個字跳過，不列入 success_count 計算
        else:
            print(f"✨ 發現新單字 '{target_word}'，開始產製...")
            should_generate = True

        if should_generate:
            try:
                # 啟動 LangGraph 多代理人工廠 (包含 Assessor -> Teacher -> Examiner -> Reviewer)
                final_state = app.invoke({"current_word": target_word, "news_context": context})
                
                # 🌟 攔截機制：如果評估員判定太簡單，直接跳過不存檔
                if not final_state.get("is_suitable", False):
                    print(f"   🛑 淘汰 '{target_word}' (難度不符或太簡單)。")
                    continue 
                
                # 從 final_state 取出經過 QA 總編輯潤飾後的最終教材
                teacher_card = final_state.get('teacher_card', '')
                quiz = final_state.get('quiz', '')
                
                if is_update:
                    SupabaseManager.update_generation_result(target_word, context, teacher_card, quiz, current_count)
                    print(f"   ✅ '{target_word}' 更新完成！")
                else:
                    SupabaseManager.save_new_generation(target_word, context, teacher_card, quiz)
                    print(f"   ✅ 新單字 '{target_word}' 已存入雲端。")
                    
                # 只有當教材被成功存進 Supabase，計數器才 +1
                success_count += 1 
                
            except Exception as e:
                print(f"   ❌ 處理 '{target_word}' 發生錯誤: {e}")

# ==========================================
# 6. 主排程器 (Main Execution)
# ==========================================
def main():
    print("🚀 系統啟動：開始執行 NOVA 每日採集排程...")
    
    # 1. 讀取每日目標總配額
    daily_quota = int(os.getenv("TARGET_DAILY_COUNT", 3))
    
    # 2. 檢查今天已經用掉了多少配額
    already_added = SupabaseManager.get_today_added_count()
    
    # 3. 計算今天還剩下多少額度可以用
    remaining_quota = daily_quota - already_added
    
    if remaining_quota <= 0:
        print(f"🛑 今日配額已滿 ({already_added}/{daily_quota})。為了你的學習品質與 API 預算，今天不再抓取新單字。")
        return
    
    print(f"📊 今日狀態：已完成 {already_added} 個，尚有 {remaining_quota} 個名額。")
    
    # 4. 抓取材料
    df_material = fetch_and_extract_daily_vocab()
    
    # 5. 執行產線，只填滿「剩餘」的配額
    mass_produce_flashcards_with_refresh(df_material, target_daily_count=remaining_quota)
    
    print("🎉 NOVA 每日採集排程執行完畢！")

if __name__ == "__main__":
    main()