---
name: tdd
description: "Use when implementing features or fixing bugs. Write tests first, then implementation. Ensures correctness and prevents regressions."
---

# Test-Driven Development

Follow this cycle strictly:

## 1. Write the Test First
- Write a failing test that describes the expected behavior
- Run it — confirm it fails for the right reason
- The test IS the specification

## 2. Write Minimal Implementation
- Write the simplest code that makes the test pass
- No extra features, no premature optimization
- Just make the test green

## 3. Refactor
- Clean up the implementation
- Remove duplication
- Improve naming
- Run tests again — must still pass

## Rules
- Never write implementation before a test
- One behavior per test
- Test the interface, not the implementation
- Follow existing test patterns in the repo (check `tests/` directory)
- Use the project's test framework (pytest, jest, etc.)
