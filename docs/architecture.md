# Architecture boundary notes

The target dependency direction is:

~~~text
External Platform
        |
        v
RPC Adapter -> Application / Agent Service -> Agent Harness -> Agent Loop
                                                        /          \
                                               LLMProvider      ToolExecutor
                                                                      |
                                                                      v
                                                                     MCP
~~~

The Agent Loop is the small model/tool/result execution algorithm. The
Agent Harness owns Run lifecycle, execution state, limits, cancellation,
and event coordination. RPC, persistence, provider SDK, MCP SDK, and
factory-specific tools remain outside the loop.

Phase 0 only establishes tooling and these dependency rules. Concrete
RPC/MCP transports, persistence technology, real provider, execution
defaults, and detailed runtime contracts remain deferred to their
relevant phases.

