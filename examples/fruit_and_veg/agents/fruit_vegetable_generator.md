---
name: Fruit and Vegetable Generator
description: Adds one interesting food for the classifier to sort.
x-role: worker
x-learning: false
---

Create exactly one new task in the Fruit and Vegetable Sorter workstream supplied in your run context.

Choose a recognizable fruit or vegetable that is not already on the board. Prefer an interesting or mildly debatable example. Create the task in the workstream's initial `Unsorted` state with:

- the food name as the title;
- a one-sentence neutral description that does not reveal the answer;
- no `fruit` or `vegetable` tag.

Use `orc task create <workstream_id> --title "<food>" --description "<neutral description>"`. Do not classify or move the task. The workstream trigger will run the classifier.