from openai_codex import Codex, Sandbox

with Codex() as codex:
    thread = codex.thread_start(
        model="gpt-5.6-terra",
        sandbox=Sandbox.read_only,
    )

    result = thread.run(
        "Respond exactly with FACTORY_OK. "
        "Do not inspect files and do not use tools."
    )

    print(result.final_response)
