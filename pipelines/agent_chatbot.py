import asyncio
import os
from pathlib import Path
from typing import Annotated, Sequence, TypedDict
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph import graph
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, START
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from dotenv import load_dotenv 
load_dotenv()

# SCRIPT_DIR = Path(__file__).resolve().parent
# RAG_SERVER_PATH = SCRIPT_DIR / "rag_server_2.py"

google_api_key = os.getenv("GOOGLE_API_KEY", "").strip()

if not google_api_key:
    raise ValueError("GOOGLE_API_KEY not found. Please set it in your .env file.")

# Define the state of the graph 
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]

def print_clean_response(message: BaseMessage):
    """
    [CHANGE B] Extracts and prints only the text content from an AI message.
    Raw messages contain list of dicts with 'type', 'text', and 'extras'
    (Gemini adds a cryptographic signature in 'extras' — we discard it).
    """
    content = message.content

    if isinstance(content, list):
        # content is a list of blocks e.g. [{'type': 'text', 'text': '...', 'extras': {...}}]
        text_parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        clean_text = "\n".join(text_parts).strip()
    else:
        # content is already a plain string
        clean_text = str(content).strip()

    if clean_text:
        print(f"\nAssistant: {clean_text}\n")

async def main():
    # [CHANGE C] Connect to the persistent SSE server instead of spawning a subprocess.
    # rag_server.py must already be running in a separate terminal:
    #   python rag_server.py
    # This connects in ~ms vs ~8s for stdio cold start.
    mcp_config = {
        "rag": {"url": "http://127.0.0.1:8000/sse", "transport": "sse"}
    }

    client = MultiServerMCPClient(mcp_config)
    all_tools = await client.get_tools()
    print(f"Successfully loaded {len(all_tools)} tools from MCP servers!\n")

    #intialize the LLM and bind tools to it
    llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key = google_api_key
    )
    
    model = llm.bind_tools(all_tools)

    # 5. Define the Agent Node
    async def call_model(state: AgentState):
        system_prompt = SystemMessage(content=
        """
            You are a helpful AI assistant backed by a private knowledge base
            that contains documents, resumes, reports, and other files.

            RULES — follow these strictly:
            1. For ANY question that requires specific facts, names, skills,
            dates, or information of any kind — you MUST call the
            answer_question tool. No exceptions.
            2. NEVER answer factual questions from your own training knowledge.
            The knowledge base is your only source of truth.
            3. If the tool returns empty or insufficient context, say so honestly.
            Do not fill gaps with your own assumptions.
            4. Only answer from your own knowledge for casual conversation
            (greetings, clarifications, meta questions about yourself).

            When in doubt — use the tool.
                
        """
        )
        # Pass the conversation history to the model
        response = await model.ainvoke([system_prompt] + state["messages"])
        return {"messages": [response]}
    
    #build the langgraph react loop 
    graph = StateGraph(AgentState)

    #add nodes 
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(all_tools))

    #add edges with conditions
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    
    #Langgraph provides various checkpointer implementations to save execution traces, intermediate states, and results.
    # Here we use the MemorySaver which keeps everything in memory and prints it at the end. 
    checkpointer = MemorySaver()
    app = graph.compile(checkpointer=checkpointer)

    thread_config = {"configurable": {"thread_id": "session_1"}}
    print("Chat started. Type 'exit' or 'quit' to end.\n")


    while True:
            query = input("You: ").strip()

            if not query:
                continue
            if query.lower() in ("exit", "quit"):
                print("Ending session.")
                break

            inputs = {"messages": [HumanMessage(content=query)]}

            final_ai_message = None
            async for event in app.astream(inputs, config=thread_config, stream_mode="values"):
                last_msg = event["messages"][-1]
                if isinstance(last_msg, AIMessage):
                    final_ai_message = last_msg

            if final_ai_message:
                print_clean_response(final_ai_message)

if __name__ == "__main__":
    asyncio.run(main())
    
