# agents/project_manager.py
from crewai import Agent, Task, Crew, Process
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
import os

load_dotenv()

# 初始化大模型（后续可换成 Claude 或其他）
llm = ChatOpenAI(
    model="gpt-4o",
    temperature=0.2
)

# ===================== 定义 AI 员工 =====================

project_manager = Agent(
    role="营养平台项目经理",
    goal="统筹整个营养平台项目，拆解任务、分配给专业Bot、跟踪进度，并生成清晰的待老板审批清单",
    backstory="""你是一个经验丰富的项目经理，擅长把复杂业务拆成可执行任务。
你不亲自做专业内容，而是清晰下达指令、协调各专业角色、检查质量。
所有对外发布、真实供应链对接、核心知识库修改，必须经过老板明确授权后才能执行。""",
    verbose=True,
    allow_delegation=True,
    llm=llm
)

system_engineer = Agent(
    role="系统工程师",
    goal="负责营养平台的技术架构、数据流设计、API对接和知识库存储方案",
    backstory="你是资深系统架构师，专注把业务需求落地成稳定、可扩展的技术系统。",
    verbose=True,
    llm=llm
)

senior_nutritionist = Agent(
    role="资深营养师",
    goal="维护专业营养知识库和真实案例库，输出科学、可落地的营养方案",
    backstory="你是持证资深营养师，所有建议必须基于证据，并标注依据来源。",
    verbose=True,
    llm=llm
)

content_designer = Agent(
    role="文案与设计负责人",
    goal="根据营养师输出，生产高质量社交媒体图文，并准备定时发布内容",
    backstory="你擅长把专业内容转化成吸引人的短视频脚本、图文和海报文案。",
    verbose=True,
    llm=llm
)

supply_chain = Agent(
    role="供应链解决方案专家",
    goal="根据营养方案，设计对应的产品供应链体系和可执行采购/合作方案",
    backstory="你熟悉健康食品、补剂、定制餐供应链，能给出性价比高且合规的解决方案。",
    verbose=True,
    llm=llm
)

# ===================== 定义任务示例 =====================

task_plan = Task(
    description="""老板提出要启动一个「职场高压人群抗疲劳营养方案」项目。
请你作为项目经理完成以下工作：
1. 拆解成各专业角色可执行的任务
2. 明确每个角色的交付物
3. 委派任务并收集初步结果
4. 最终输出一份《项目进度与待审批事项》给老板""",
    expected_output="清晰的项目计划 + 各角色交付物汇总 + 需要老板授权的事项清单",
    agent=project_manager
)

# ===================== 组建团队 =====================

crew = Crew(
    agents=[project_manager, system_engineer, senior_nutritionist, content_designer, supply_chain],
    tasks=[task_plan],
    process=Process.hierarchical,  # 层级模式，项目经理自动成为管理者
    manager_llm=llm,
    verbose=True
)

# ===================== 运行入口 =====================

if __name__ == "__main__":
    print("开始运行营养平台 AI 员工团队...\n")
    result = crew.kickoff()
    print("\n\n========== 最终输出（交给老板审批） ==========\n")
    print(result)
