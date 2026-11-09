# agents/senior_nutritionist.py
from crewai import Agent, Task, Crew, Process
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
import os

load_dotenv()

# 模型配置（后续可改成 DeepSeek 等）
llm = ChatOpenAI(
    model="gpt-4o-mini",      # 先用便宜模型，后续可换成 deepseek-chat 等
    temperature=0.3
)

# ===================== 资深营养师 Bot =====================

senior_nutritionist = Agent(
    role="资深营养师",
    goal="基于可靠营养知识，为用户提供科学、严谨、可落地的营养建议，并明确提示所有个性化建议需由真人营养师最终确认",
    backstory="""你是一名持证资深营养师，具备扎实的临床营养和公共营养背景。
你严格遵循中国居民膳食指南及国家卫健委相关食养指南。
你不会做出确定性的医疗诊断，也不会替代医生或注册营养师的最终决策。
所有涉及个人化营养方案的内容，你都会明确提醒用户需经专业营养师审核确认。""",
    verbose=True,
    allow_delegation=False,
    llm=llm
)

# ===================== 回答任务模板 =====================

def create_nutrition_task(user_question: str):
    return Task(
        description=f"""
用户问题：{user_question}

请按照以下要求回答：
1. 基于科学营养知识给出清晰、结构化的回答
2. 如果涉及个性化建议，必须说明“以上建议仅供参考，具体方案需由专业营养师根据您的实际情况确认”
3. 遇到可能的疾病风险或超出营养服务范围的问题，明确建议用户寻求医生帮助
4. 回答尽量简洁实用，避免过于学术化
5. 不要使用绝对化承诺（如“一定能减肥”“保证降血糖”等）
""",
        expected_output="结构清晰、带有必要风险提示和确认声明的营养回答",
        agent=senior_nutritionist
    )

# ===================== 运行入口 =====================

if __name__ == "__main__":
    print("资深营养师 Bot 已启动（测试模式）\n")
    
    # 测试问题，可自行修改
    test_question = "我最近想控制体重，主食应该怎么吃比较好？"
    
    task = create_nutrition_task(test_question)
    
    crew = Crew(
        agents=[senior_nutritionist],
        tasks=[task],
        process=Process.sequential,
        verbose=True
    )
    
    result = crew.kickoff()
    
    print("\n\n========== 营养师回答 ==========\n")
    print(result)
