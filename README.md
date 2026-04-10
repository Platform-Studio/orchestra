# Orchestration Thoughts
JB, Apr 9, 2026

## Introduction
***tl;dr - let agents write their own orchestration framework.***

For agent orchestration, I'm realizing that I've been thinking like a traditional software engineer, not a vibe coder or spec-driven development engineer.

I've been spending time looking at the many existing open source orchestration frameworks and trying to determine which, if any, I should use.

At the time of writing, there are many strong choices, and things are moving quickly, including:
- CrewAI (https://github.com/crewaiinc/crewai) - open source orchestration framework built by CrewAI, a YC company.
- Ruflo (https://github.com/ruvnet/ruflo) - another open source orchestration framework with a focus on simplicity and flexibility.
- GasTown (https://github.com/gastownhall/gastown) - a lightweight orchestration framework designed for rapid development.

They all are impressive and have different strengths. They each have distinct opinions, sometimes strong ones. There's of course also something to be said for having something concrete that just works.

However, I'm realizing that the true spec-driven way to approach this is to define how I think orchestration should work at a high level, and then have the agents actually write the code.

This feels quite meta, but I think it may lead to a better result. The enabling technologies will likely continue to evolve quickly, so it will be ok - and potentially essential - to reimplement the framework multiple times as needed. The key is to have a clear spec for how it should work and then let the agents figure out the best way to implement it.

The data is the absolute foundation. As long as you have a persisted record of the work that agents need to do, are doing, and have done, and the artifacts they have generated, the orchestration mechanics can be iterated on.

I want to keep everything as simple and loosely-coupled as possible.

## Files
- `spec.md` - the core specification of the orchestration framework, defining the core concepts
- `implementation_guidance.md` - guidance for how to implement the orchestration framework, including recommended data schemas and interfaces for some of the key concepts
- `implementation_plan_python.md` - a specific implementation plan for implementing the orchestration framework in Python, using the local file system for data persistence. You can see an actual concrete implementation of this plan in https://github.com/Platform-Studio/orchestra-python

## Hierarchy
Here's how to think about the hierarchy:

```mermaid
flowchart LR
    A[Spec] --> B[Implementation Guidance] --> C[Implementation Plan] --> D[Implementation]
```

## Writing your own implementation plan
Of course, you should get the AI to write, or at least review, its own implementation plan! 

Point your agent of choice at `spec.md` and `implementation_guidance.md` and ask it to write an implementation plan for how to implement the orchestration framework defined in the spec using whatever language, persistence layer, etc you wish.