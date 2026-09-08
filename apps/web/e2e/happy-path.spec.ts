import { expect, test } from "@playwright/test";

// The one Playwright happy path the hardening phase calls for: sign up, sign in, create a
// workspace, land on it. Runs against a real API/Postgres/Redis (see playwright.config.ts) —
// there's no faking a session cookie or a workspace membership from the browser side, so this
// is the actual account lifecycle a new user goes through, not a mocked stand-in for it.
//
// It stops short of sending a chat message: that needs a real, working provider credential,
// which (same as every other phase's testing notes) this repo can't ship one of.
test("sign up, sign in, and create a workspace", async ({ page }) => {
  const email = `e2e-${Date.now()}@example.com`;
  const password = "correct horse battery staple";

  await page.goto("/signup");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();

  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByText("Account created")).toBeVisible();

  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL("/");
  await expect(page.getByText("api up")).toBeVisible();

  await page.goto("/workspaces/new");
  await page.getByLabel("Name").fill("E2E Workspace");
  await page.getByRole("button", { name: "Create workspace" }).click();

  await expect(page).toHaveURL(/\/w\/e2e-workspace/);
  await expect(page.getByRole("heading", { name: "E2E Workspace" })).toBeVisible();
  await expect(page.getByText("No conversations yet")).toBeVisible();
});
