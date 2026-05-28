# Sandbox adapter (opt-in)

Adapter scaffold for delegating heavy / containerized workloads to a local
sandbox. Requires Docker or WSL on the host. Disabled by default.

To wire one up: implement a `SandboxRunner` class that exposes
`run(image, command, env, mounts) -> {stdout, stderr, exit_code}` and register
it from your bootstrap code.
