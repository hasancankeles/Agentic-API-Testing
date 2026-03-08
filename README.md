# Project Proposal & Work Plan: Agentic API Testing Software

**Team Members:** Hasancan & Eren  
**Term:** Spring 2026 (Started February 9, 2026)  
**Important Dates:**  
- Midterm Report: March 30, 2026  
- Final Report & Poster Presentation: June 11, 2026  

---

## 1. Project Overview

For our term project, we are developing an Agentic API Testing Software, heavily inspired by modern platforms like Kusho AI. Traditional API testing tools (like Postman or REST Assured) require developers to manually write and maintain test cases. Our goal is to build an intelligent, autonomous tool that leverages Large Language Models (LLMs) to automatically generate, execute, and self-heal API test suites.

Instead of just acting as a simple text generator, our software will function as an "agent." It will read API specifications, plan test scenarios (covering happy paths, edge cases, and negative tests), generate dynamic assertions, and actually execute the HTTP requests to validate the endpoints.

## 2. Core Features & Scope

We have scoped out the following core features for our MVP (Minimum Viable Product):

- **Intelligent Spec Ingestion:** The system will accept OpenAPI/Swagger files (JSON/YAML) or raw cURL commands and parse them into an internal data model.
- **Agentic Test Generation:** Using an LLM, the software will automatically write comprehensive test suites based on the ingested API structure.
- **Dynamic Assertion Engine:** The AI will automatically generate validation rules (e.g., expected status codes, JSON schema validation, data types).
- **Natural Language Editing:** Users will be able to modify or add specific tests using plain English (e.g., *"Add a test to ensure the API returns a 401 if the auth token is missing"*).
- **Execution & Reporting:** A built-in runner to execute the generated payloads against target APIs and log the pass/fail results.

## 3. Tech Stack

- **Backend & Core Logic:** Python (FastAPI) 
- **AI / Agent Framework:** LangChain (interacting with OpenAI or Anthropic APIs)
- **Execution Engine:** Python `requests` and `pytest`
- **Frontend/Interface:** A lightweight Web UI or an interactive Command Line Interface (CLI)

## 4. Division of Labor

To ensure we meet our deadlines and work efficiently in parallel, we have divided the core responsibilities:

**Hasancan (AI & Agentics Lead)**
- Integrating LangChain and managing LLM prompts.
- Developing the "Test Plan Generator" and "Assertion Engine."
- Implementing the Natural Language Editor features.

**Eren (Backend & Execution Lead)**
- Building the OpenAPI/Swagger parsing logic.
- Developing the core Execution Engine to fire the actual HTTP requests.
- Handling environment variables, authentication, and test result logging.

*Note: UI/CLI development, QA testing, and academic reporting will be shared responsibilities.*

## 5. Project Timeline (Spring 2026)

**Phase 1: Foundation & Parsing (Feb 9 – Mar 8)**
- Finalize system architecture and repository setup.
- Build parsers to convert OpenAPI specs into internal JSON objects.
- Setup LLM API connections and test basic prompt completions.

**Phase 2: Core Generation & Midterm Prep (Mar 9 – Mar 29)**
- Feed parsed data to the LLM to successfully generate test payloads and assertions.
- Draft the Midterm Report, documenting our architecture, parsing logic, and initial generation outputs.
- **Deliverable:** Midterm Report (Due: March 30)

**Phase 3: Execution Engine (Mar 31 – Apr 19)**
- Build the HTTP runner to execute the AI-generated tests.
- Implement variable management (e.g., dynamically passing a generated auth token to subsequent tests).

**Phase 4: Natural Language Loop & Refinement (Apr 20 – May 10)**
- Integrate the conversational prompt so users can edit tests via text.
- Implement basic self-correction (e.g., if the AI spots a formatting error in its own JSON payload, it retries).

**Phase 5: Reporting & System Testing (May 11 – May 31)**
- Build the test report generator (showing total tests, coverage, and logs).
- Thoroughly test the system against dummy public APIs (like Swagger Petstore or ReqRes).
- Squash bugs and stabilize the codebase.

**Phase 6: Final Documentation & Presentation (Jun 1 – Jun 10)**
- Draft the Final Report according to university guidelines.
- Design the academic poster.
- Prepare and rehearse the live demo scenario.
- **Deliverable:** Final Report & Poster Presentation (Due: June 11)

---
*Prepared by Hasancan and Eren for Spring 2026 Term Project.*