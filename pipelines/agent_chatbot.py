import asyncio
import os
from pathlib import Path
from typing import Annotated, Sequence, TypedDict
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, START
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_mcp_adapters.client import MultiServerMCPClient
from dotenv import load_dotenv 
load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent
RAG_SERVER_PATH = SCRIPT_DIR / "rag_server.py"

google_api_key = os.getenv("GOOGLE_API_KEY", "").strip()

if not google_api_key:
    raise ValueError("GOOGLE_API_KEY not found. Please set it in your .env file.")

# Define the state of the graph 
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]

async def main():
    # Connect to the local MCP RAG server.
    mcp_config = {
        "rag": {"command": "python", "args": [str(RAG_SERVER_PATH)], "transport": "stdio"}
    }

    client = MultiServerMCPClient(mcp_config)
    all_tools = await client.get_tools()
    print(f"Successfully loaded {len(all_tools)} tools from MCP servers!\n")

    #intialize the LLM and bind tools to it
    llm = ChatGoogleGenerativeAI(
            model="gemini-3-flash-preview",
            google_api_key = google_api_key
            )
    
    model = llm.bind_tools(all_tools)

    # 5. Define the Agent Node
    async def call_model(state: AgentState):
        system_prompt = SystemMessage(content=
        """
        You are a highly capable AI assistant connected to various tools. 
        Use your tools to gather information or calculate data before answering the user.
        If you need to use multiple tools, do so.
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
    
    app = graph.compile()

    query = input("Ask a question: ").strip()
    if not query:
        print("No question provided. Exiting.")
        return

    print(f"User: {query}\n")
    print("--- Execution Log ---")
    
    inputs = {"messages": [HumanMessage(content=query)]}
    async for event in app.astream(inputs, stream_mode="values"):
        message = event["messages"][-1]
        message.pretty_print()


if __name__ == "__main__":
    asyncio.run(main())
    
