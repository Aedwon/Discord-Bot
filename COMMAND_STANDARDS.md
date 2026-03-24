# MLBB Discord Bot: Slash Command Standards

This document establishes the strict architectural naming conventions and grouping requirements for all slash commands in the MLBB bot. **All future commands must adhere to these rules.**

## 1. Core Philosophy: "The Group Standard"
Discord provides up to 3 tiers of nesting for App Commands: 
`/[Group Name] [Sub-Group Name] [Command Name]`

We strictly utilize App Command Groups (`@app_commands.Group`) to organize feature sets. 
**Rule:** Top-level flat commands (`/prefix-command`) are strictly forbidden unless they are universal global utilities (e.g., `/help`, `/ping`).

### Incorrect vs. Correct
❌ **Incorrect (Flat & Cluttered):** 
`/xp-add`, `/xp-remove`, `/setup-tickets`, `/autocreate_setup`, `/dl_embed`

✅ **Correct (Grouped & Clean):**
`/xp add`, `/xp remove`, `/ticket setup`, `/voice setup`, `/embed download`

## 2. Naming Conventions

### 2.1 Character Restrictions
- **No Underscores:** Never use `_` in a command name visible to the user. Discord natively prefers spaces and hyphens. Use spaces to denote groups.
- **Lowercase Only:** Discord requires all slash commands to be lowercase.
- **Action Verbs for Subcommands:** The terminal command node must be an action verb (e.g., `add`, `remove`, `set`, `view`, `toggle`).

### 2.2 Standardized Identifiers
When creating CRUD (Create, Read, Update, Delete) equivalents, always use the following exact verbiage:
- **Addition:** `add` (Not `give`, `grant`, or `append`)
- **Removal:** `remove` (Not `delete`, `revoke`, or `take`)
- **Modification:** `set` (Not `change`, `edit`, or `update`)
- **Inspection:** `view` or `status` (Not `check`, `show`, or `inspect`)
- **Boolean Switches:** `toggle` (Not `enable` / `disable`)

## 3. Security Paradigms

**Rule:** Every single command that forcefully alters persistent state (Economy amounts, Placements, Settings, Setup Operations) MUST inject the `@require_admin_auth()` decorator immediately beneath the `@command` decorator.

```python
# ✅ CORRECT IMPLEMENTATION
@xp_group.command(name="add", description="Force add XP to a user")
@require_admin_auth() # Requires the 15-minute Admin Session Modal to be active!
async def xp_add(self, interaction: discord.Interaction, target: discord.Member, amount: int):
    ...
```

## 4. Required Core Groups
If you are developing a new feature, map it into one of these existing top-level namespaces:
- `/xp [...]` (Leveling & Text/Voice Activity tracking)
- `/ep [...]` (Event Points & Official Tournament Economy)
- `/event [...]` (Live Event tools, placement caps, kiosks)
- `/ticket [...]` (Support ticket routing & stats)
- `/embed [...]` (Discohook interactions)
- `/analytics [...]` (Data visualization & metrics)
- `/setup [...]` (Global backend variables, channels, & roles)
- `/admin [...]` (Core security, auth, and cache wiping)

If the feature covers a completely new domain, instantiate a new `app_commands.Group` at the top of the Cog class.
