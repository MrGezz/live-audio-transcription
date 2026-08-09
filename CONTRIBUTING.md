# Contribution Guidelines

Thank you for considering contributing! To ensure a smooth review process and respect everyone's time, please follow these rules when submitting code.

## 1. Git Workflow & Branching
* **Never submit a PR from your `main` branch.** Always create a dedicated feature branch for your work (e.g., `feature/add-api-fix` or `fix/encoding-bug`).
* **Keep PRs focused.** One PR should address a single feature or bug fix. Avoid mixing unrelated changes into a single request.

## 2. Code Readiness & Review Process
* **Code Freeze During Review:** Once you submit a PR for review, **do not push additional commits** unless directly responding to reviewer feedback. Pushing continuous updates mid-review invalidates existing code suggestions and wastes the reviewer's time.
* **Use Draft PRs:** If your code is still a work-in-progress, being actively tested, or being iterated on, open it as a **Draft Pull Request**. Only convert it to "Ready for Review" when the code is final.
* **Test Before Submitting:** Ensure the code runs, builds, and is tested locally on your machine before requesting a review.

## 3. Responsible AI Code Generation
If you use AI tools to assist your development:
* **Understand what you push:** You are entirely responsible for the code you submit. If you cannot explain the logic, syntax, or architecture of your PR, it will be rejected.
* **No live AI-prompting on open PRs:** Do not use open PRs as a testing ground to repeatedly dump unverified AI output. Iterate with your AI locally on your own machine *before* opening a PR.
