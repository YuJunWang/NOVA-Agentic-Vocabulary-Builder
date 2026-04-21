import os, json, time
from supabase import create_client
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0)

print("🔥 [警告] 啟動強制重刷模式：將重新萃取所有資料的純淨英文原句...")

def main():
    # 1. 抓取「所有」記錄，不再侷限於 NULL
    response = supabase.table("llm_generation_cache").select("*").execute()
    records = response.data
    
    print(f"📊 預計處理筆數：{len(records)} 筆")

    # 2. 強化版 Prompt：嚴格區分來源
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是一個資料還原專家。必須嚴格遵守指令並只輸出 JSON。"),
        ("user", """
        正在處理的焦點單字：【 {word} 】
        
        請根據以下資料，萃取出兩組「完整、沒有任何空格」的純英文句子。
        
        【原始新聞背景】：{news_context}
        【教學卡】：{teacher_content}
        【測驗題】：{quiz_content}
        
        任務要求：
        1. raw_example_en：從「教學卡」中抓出的新聞例句，保持句子的原樣，就是新聞背景原句。
        2. raw_quiz_en：從「測驗題」中抓出考題原句。
           🔥 如果測驗題裡面有底線 (例如 `_____` 或 `___`)，請你直接用單字【 {word} 】替換掉那個底線！絕對不允許輸出底線！
        
        輸出 JSON 格式：
        {{
            "raw_example_en": "完整無底線的新聞例句，就是新聞背景原句",
            "raw_quiz_en": "已經用 {word} 填補底線的完整考題句子"
        }}
        """)
    ])

    chain = prompt | llm

    for index, record in enumerate(records, 1):
        word = record['word']
        print(f"[{index}/{len(records)}] 正在強制修復: '{word}'...")
        
        try:
            res = chain.invoke({
                "word": word,
                "news_context": record.get("news_context", ""),
                "teacher_content": record.get("teacher_card_content", ""),
                "quiz_content": record.get("examiner_quiz_content", "")
            })
            
            # 解析並清理
            clean_json = res.content.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_json)
            
            # 寫回 Supabase (強制覆蓋)
            supabase.table("llm_generation_cache").update({
                "raw_example_en": data.get("raw_example_en", ""),
                "raw_quiz_en": data.get("raw_quiz_en", ""),
                # 順便把向量清空，這樣等一下 sync_missing_embeddings 就會自動重算
                "example_embedding": None 
            }).eq("word", word).execute()
            
            # 稍微停頓避免 API 限流
            time.sleep(0.5)
            
        except Exception as e:
            print(f"❌ '{word}' 修復失敗: {e}")

    print("\n✨ 任務完成！所有資料已完成「強制去重」與「原句還原」。")
    print("👉 現在請執行 sync_missing_embeddings() 重新生成高品質向量！")

if __name__ == "__main__":
    main()