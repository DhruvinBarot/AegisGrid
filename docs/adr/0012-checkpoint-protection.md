# ADR-0012: Checkpoint protection via envelope encryption with a vetted library

**Status:** Accepted 2026-10-26. The **library choice is provisional** and gets verified at the start of Phase 3.

**Supersedes:** the validation-review wording "our own AES-GCM cipher". The human rejected it on 2026-10-26.

## Context

LangGraph checkpoints hold full workflow state, which can include prompts, tool outputs and PII. I checked the source of `langgraph-checkpoint-postgres` 3.2.0 (main branch, 2026-10-26) and found:

- `EncryptedSerializer` encrypts only data passing through `serde.dumps_typed`, meaning blobs and pending writes. It carries no key id, so keys can't be rotated.
- `put()` stores primitive channel values (`None`, `str`, `int`, `float`, `bool`) **inline as plaintext JSONB** in `checkpoints.checkpoint`. These bypass the serializer entirely.
- Checkpoint `metadata` is plaintext JSONB, and string or number values from `config["configurable"]` and `config["metadata"]` get copied into it.
- The library's tables have no `organization_id` column, so RLS can't cover them.

## Decision

1. **No custom cryptographic primitives.** All encryption is delegated to a vetted, maintained library that does **envelope encryption**: data keys wrapped by a managed key service.
   - **Preferred:** AWS Encryption SDK for Python.
     - Production uses the KMS keyring.
     - Local dev uses a raw AES keyring with local key material, so it's free. It's the same library and the same message format.
     - Data-key caching limits KMS calls, which are billed per request.
   - **Alternative:** Google Tink with its AWS KMS integration.
   - Phase 3 verifies the pick: Python 3.13 support, maintenance status, caching behavior, and an exactly pinned version.
2. **What AegisGrid owns:**
   - `CheckpointCipher`, an adapter that implements LangGraph's `CipherProtocol` and delegates to the library. It never calls AES directly.
   - Cipher-name and key-id metadata, for example `esdk:v1`.
   - Rotation policy: new data keys under the new KMS key; old keys remain decrypt-only; re-wrap on rotation.
   - The serialization boundary.
   - An **encryption context** of `{org_id, execution_id}`, taken from a worker contextvar. This cryptographically binds each blob to its tenant and execution.
3. **State-schema rule**, enforced by the spec compiler: every top-level state channel is a container (dict, list or messages). Primitive top-level channels are allowed only for reserved, non-sensitive runtime fields.
4. **No content in run `config`.** Only `thread_id = execution_id`.
5. **Isolation:**
   - Tables live in a `runtime` schema that only `aegis_worker` can access (ADR-0004).
   - Checkpoints are never exposed through the API.
   - They are deleted 7 days after the execution reaches a terminal state, or immediately on erasure.
6. **Canary test:** a run writes a unique canary string, and the test asserts it never appears in plaintext anywhere in the `runtime` schema.

## Consequences

- Workflow content is encrypted at rest beyond what RDS provides, and blobs are cryptographically bound to their tenant.
- KMS adds per-request cost in production. Data-key caching keeps that bounded.
- Spec authors can't use bare top-level strings in state. The compiler error explains why.
- If a future LangGraph version changes its inline-storage behavior, the canary test detects it.

## Alternatives rejected

- **Implementing AES-GCM in our own code:** rejected by the human. Nonce and key handling are easy to get wrong.
- **LangGraph's `EncryptedSerializer` alone:** no key id or rotation support, and it doesn't cover inline primitives.
- **Wrapping the saver to add `organization_id`:** high maintenance cost for low practical gain.
