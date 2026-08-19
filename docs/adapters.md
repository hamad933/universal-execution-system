# Adapter Registry and Repository Overrides

UES uses composition rather than one oversized universal environment.

## Resolution order

1. Load the trusted Universal Adapter Registry.
2. Select the declared project family.
3. Add capabilities detected from repository signals.
4. Add explicitly declared project capabilities.
5. Remove `disabled_capabilities`.
6. Discover repository-native scripts where supported.
7. Apply repository command overrides last.

Repository overrides win. Unknown capabilities are reported but are not loaded or executed.

## Safety

Adapter planning is read-only. It may inspect repository metadata such as `package.json` and `composer.json`, but it does not install dependencies or execute discovered commands.

The `/exec adapter-plan` bridge accepts only a repository-relative contract path. Path traversal outside the checked-out repository is rejected.

## Lean-by-default rule

A project receives only the capability packs it detects or declares. A Python project does not inherit Node, PHP, browsers, databases, or mobile tooling merely because UES supports them elsewhere.

## Extension model

The central registry defines reusable families and capability packs. A repository can customize:

- `adapter.family`
- `adapter.capabilities`
- `adapter.disabled_capabilities`
- `adapter.commands`

Command overrides may use an argv array, which is preferred for future typed execution, or a shell string for compatibility. The current adapter layer only produces a plan; it never executes these commands.

Future project-type adapters can add richer resolvers without changing the universal core contract.
