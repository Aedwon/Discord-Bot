const DB_DATA = [
    {
        "category": "Bot Setup & General Config",
        "emoji": "\u2699\ufe0f",
        "id": "setup",
        "features": [
            {
                "name": "Automatic Admin Access",
                "type": "passive",
                "desc": "Users with explicit Administrator permissions bypass rate limits and gain global bot access. Sensitive commands additionally require /admin auth session login."
            }
        ],
        "commands": [
            {
                "syntax": "/setup view",
                "desc": "View all current bot settings & setup checklist showing configured channels, roles, and cosmetics.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/setup channel",
                "desc": "Map a functional text channel (logs, counting, confessions, anon messages, etc).",
                "access": "admin",
                "params": [
                    {
                        "name": "setting",
                        "type": "choice",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup role",
                "desc": "Map a functional role (server booster, veteran, muted, restricted, verified, support).",
                "access": "admin",
                "params": [
                    {
                        "name": "setting",
                        "type": "choice",
                        "required": true
                    },
                    {
                        "name": "role",
                        "type": "@role",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup vc",
                "desc": "Map a voice channel (e.g., booster lounge).",
                "access": "admin",
                "params": [
                    {
                        "name": "setting",
                        "type": "choice",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "voice",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup color-add",
                "desc": "Add a booster color role to the palette.",
                "access": "admin",
                "params": [
                    {
                        "name": "name",
                        "type": "string",
                        "required": true
                    },
                    {
                        "name": "role",
                        "type": "@role",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup color-remove",
                "desc": "Remove a booster color role from the palette.",
                "access": "admin",
                "params": [
                    {
                        "name": "name",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup color-list",
                "desc": "List all configured booster color roles.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/setup emblem-add",
                "desc": "Add a booster emblem role.",
                "access": "admin",
                "params": [
                    {
                        "name": "emoji",
                        "type": "string",
                        "required": true
                    },
                    {
                        "name": "role",
                        "type": "@role",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup emblem-remove",
                "desc": "Remove a booster emblem role.",
                "access": "admin",
                "params": [
                    {
                        "name": "emoji",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup emblem-list",
                "desc": "List all configured booster emblem roles.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/setup xp_roles",
                "desc": "Auto-discover and map the 21 EXP Role Tiers (Commoner V \u2013 Monarch) from your server.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/setup ep_roles",
                "desc": "Auto-map all 34 EP sub-tier roles (Warrior V \u2013 Mythic ladder).",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/setup peak_roles",
                "desc": "Auto-map the 10 Peak Rank legacy roles (Peak Warrior \u2013 Peak Mythical Immortal).",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/setup quiz_channel",
                "desc": "Set the channel for automated quiz sessions (Noon & 8PM PHT daily).",
                "access": "admin",
                "params": [
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup sync_xp_roles",
                "desc": "Bulk-assign the correct XP tier role to ALL users based on their current XP.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/setup trigger_eos",
                "desc": "Force trigger End-of-Season: assign Peak Ranks, strip EP roles, reset EP, advance season.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/setup analytics_sentiment_channel",
                "desc": "Set the channel for automatic daily sentiment exports.",
                "access": "admin",
                "params": [
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup analytics_tracked_roles",
                "desc": "Set which opt-in roles to track adoption rates for.",
                "access": "admin",
                "params": [
                    {
                        "name": "role1\u2013role5",
                        "type": "@role",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup analytics_regions",
                "desc": "Set comma-separated role names representing geographic regions.",
                "access": "admin",
                "params": [
                    {
                        "name": "region_roles",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/setup wipe",
                "desc": "Permanently reset specific bot systems (XP, EP, economy, modlogs, quests, etc.) or perform a Full Server Wipe.",
                "access": "admin",
                "params": [
                    {
                        "name": "category",
                        "type": "choice",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/admin auth",
                "desc": "Authenticate via password to unlock heavy admin commands for the session.",
                "access": "admin",
                "params": [
                    {
                        "name": "password",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/admin logout",
                "desc": "End your active admin session immediately.",
                "access": "admin",
                "params": []
            }
        ]
    },
    {
        "category": "Moderation & Security",
        "emoji": "\ud83d\udee1\ufe0f",
        "id": "mod",
        "features": [
            {
                "name": "24h Experience Lock",
                "type": "passive",
                "desc": "Receiving a formal Warning instantly strips a user\u2019s ability to gain Level XP or Event Points for 24 hours."
            },
            {
                "name": "Message Log Auditing",
                "type": "passive",
                "desc": "All message edits and deletions are silently logged to the configured mod-log channel with original content cached."
            },
            {
                "name": "Native Timeout Mutes",
                "type": "passive",
                "desc": "Mutes use Discord\u2019s native timeout API \u2014 they auto-expire, persist across leaves/rejoins, and enforce a 28-day maximum."
            }
        ],
        "commands": [
            {
                "syntax": "/history",
                "desc": "View a user\u2019s entire moderation rap sheet (bans, kicks, mutes, warns) and current timeout status.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/warn",
                "desc": "Issue a formal warning. Applies a 24h XP lock automatically.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "reason",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/mute",
                "desc": "Timeout a user using Discord\u2019s native timeout API. Auto-expires, persists across rejoins. Max 28 days.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "duration",
                        "type": "time (e.g. 1d)",
                        "required": true
                    },
                    {
                        "name": "reason",
                        "type": "string",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/unmute",
                "desc": "Remove a native timeout from a member immediately.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/restrict",
                "desc": "Restrict user from posting images and embeds (role-based, for partial restrictions).",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "duration",
                        "type": "time (e.g. 1h)",
                        "required": true
                    },
                    {
                        "name": "reason",
                        "type": "string",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/unrestrict",
                "desc": "Remove image/embed restriction from a user.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/ban",
                "desc": "Ban a user. Permanent bans wipe all economy data (XP, EP, tokens).",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "duration",
                        "type": "time",
                        "required": false
                    },
                    {
                        "name": "reason",
                        "type": "string",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/unban",
                "desc": "Unban a user by their Discord ID.",
                "access": "admin",
                "params": [
                    {
                        "name": "user_id",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/kick",
                "desc": "Kick a member from the server.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "reason",
                        "type": "string",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/purge",
                "desc": "Bulk delete up to 100 messages in the current channel. Optionally filter by user.",
                "access": "admin",
                "params": [
                    {
                        "name": "amount",
                        "type": "number",
                        "required": true
                    },
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/autorole",
                "desc": "Assign the auto-role to all current members who don\u2019t have it.",
                "access": "admin",
                "params": []
            }
        ]
    },
    {
        "category": "Leveling & Event Points",
        "emoji": "\ud83d\udcc8",
        "id": "leveling",
        "features": [
            {
                "name": "Dynamic EP Conversion",
                "type": "passive",
                "desc": "Event Points map to MLBB-style rank tiers (Warrior \u2013 Mythic) with automatic role assignment on every EP change."
            },
            {
                "name": "Smart Role Sync",
                "type": "passive",
                "desc": "When users hit level milestones, old tier roles are stripped and the correct new rank role is assigned atomically."
            }
        ],
        "commands": [
            {
                "syntax": "/profile",
                "desc": "Show your unified Community Profile with XP progress, EP rank, badges, verification status, and server rank.",
                "access": "general",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/levels-leaderboard",
                "desc": "Show the top 10 members sorted by Discord Experience (XP) with your server-wide rank.",
                "access": "general",
                "params": []
            },
            {
                "syntax": "/event-leaderboard",
                "desc": "Show the top 10 most active Event attendees ranked by EP.",
                "access": "general",
                "params": []
            },
            {
                "syntax": "/xp start",
                "desc": "Enable the global XP engine to actively reward user messages.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/xp stop",
                "desc": "Disable the XP engine gracefully.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/xp status",
                "desc": "Check whether the XP system is currently running.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/xp add",
                "desc": "Add raw XP to a specific user.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "amount",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/xp set",
                "desc": "Override a user\u2019s total XP to an exact value.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "amount",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/xp reset",
                "desc": "Reset XP for one user or EVERYONE. Strips associated tier roles.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/ep add",
                "desc": "Add Event Points directly to a user.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "amount",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/ep set",
                "desc": "Override a user\u2019s Event Points to an exact value.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "amount",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/ep reset",
                "desc": "Reset EP for one user or EVERYONE.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": false
                    }
                ]
            }
        ]
    },
    {
        "category": "Analytics & Auditing",
        "emoji": "\ud83d\udcca",
        "id": "analytics",
        "features": [
            {
                "name": "Daily Automatic Sentiment Export",
                "type": "passive",
                "desc": "The bot automatically exports daily community messages to the configured analytics channel for external LLM sentiment analysis."
            },
            {
                "name": "Tracked Link Analytics",
                "type": "passive",
                "desc": "Buttons placed on announcements via /analytics track_link silently log unique and total clicks for conversion tracking."
            }
        ],
        "commands": [
            {
                "syntax": "/analytics overview",
                "desc": "7-day community health summary covering messages, retention, and peak activity.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics notifications",
                "desc": "Notification role subscription analytics showing adoption rates.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics active_users",
                "desc": "DAU, WAU, and MAU with visual trendlines and stickiness ratio.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics retention",
                "desc": "Day-1, Day-7, and Day-30 new member cohort retention rates.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics channels",
                "desc": "Top 10 channels ranked by raw message volume.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics voice",
                "desc": "Top 10 voice channels ranked by accumulated minutes.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics invites",
                "desc": "Top invite links ranked by join count.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics roles",
                "desc": "Opt-in role adoption percentages.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics peak_hours",
                "desc": "Message activity heatmap by hour and day of the week.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics event",
                "desc": "RSVP vs attendance conversion funnel for a specific Discord event.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/analytics track_link",
                "desc": "Create a tracked button on an existing announcement message.",
                "access": "admin",
                "params": [
                    {
                        "name": "message_id",
                        "type": "number",
                        "required": true
                    },
                    {
                        "name": "label",
                        "type": "string",
                        "required": true
                    },
                    {
                        "name": "url",
                        "type": "url",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/analytics link_stats",
                "desc": "View click stats for a tracked link.",
                "access": "admin",
                "params": [
                    {
                        "name": "message_id",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/analytics sentiment daily",
                "desc": "Export yesterday\u2019s messages as a downloadable .txt file.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics sentiment weekly",
                "desc": "Export last 7 days\u2019 messages as a downloadable .txt file.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics sentiment monthly",
                "desc": "Export last 30 days\u2019 messages as a downloadable .txt file.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/analytics keyword add",
                "desc": "Add a keyword to track across all server messages.",
                "access": "admin",
                "params": [
                    {
                        "name": "keyword",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/analytics keyword remove",
                "desc": "Stop tracking a keyword.",
                "access": "admin",
                "params": [
                    {
                        "name": "keyword",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/analytics keyword list",
                "desc": "Show all tracked keywords.",
                "access": "admin",
                "params": []
            }
        ]
    },
    {
        "category": "Live Event Operations",
        "emoji": "\ud83c\udfdf\ufe0f",
        "id": "tournaments",
        "features": [
            {
                "name": "Rigid Prize Pools",
                "type": "passive",
                "desc": "Event payouts enforce pre-configured prize tiers via UI modals, mathematically preventing unauthorized bonus EP limits."
            },
            {
                "name": "Automated Embed Lifecycle",
                "type": "passive",
                "desc": "The event embed organically evolves from a Registration panel into a Final Results leaderboard upon conclusion."
            },
            {
                "name": "Peak Voice Tracking",
                "type": "passive",
                "desc": "RAM-cached peak concurrent voice attendance is tracked across main + overflow channels automatically."
            }
        ],
        "commands": [
            {
                "syntax": "Step 1: /event setup-rewards",
                "desc": "Define the exact prize pool structure manually using the UI modal prior to deployment.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "Step 2: /event register",
                "desc": "Deploy the public-facing announcement embed with interactive Registration buttons and optional private threads.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": true
                    },
                    {
                        "name": "discohook_link",
                        "type": "url",
                        "required": true
                    },
                    {
                        "name": "max_participants",
                        "type": "number",
                        "required": false
                    },
                    {
                        "name": "thread_mode",
                        "type": "choice",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "Step 3: /event award",
                "desc": "(Post-Event) Natively open the dropdown UI menuboard to securely award the predefined prize pools to the correct winners.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "Step 4: /event close-registration",
                "desc": "Close the event natively. The bot automatically evolves the announcement embed into an 'Official Results' leaderboard and bulk-awards standard participation EP to all remaining registrants.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    },
                    {
                        "name": "payout_participation",
                        "type": "boolean",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/event export_winners",
                "desc": "Export an event's placement winners as a CSV file.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/event revoke",
                "desc": "Erase a false payout entirely and deduct the EP.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    },
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/event status",
                "desc": "Live dashboard showing peak VC, check-ins, budget disbursed, and placement ledger.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/event overflow add",
                "desc": "Link an additional overflow voice channel to a scheduled event for peak tracking.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "voice",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/event overflow remove",
                "desc": "Revoke an overflow voice channel mapping from an event.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "voice",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/event kiosk (Legacy)",
                "desc": "Spawn an ad-hoc Participation Check-In button (Unmanaged).",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    },
                    {
                        "name": "ep",
                        "type": "number",
                        "required": true
                    },
                    {
                        "name": "description",
                        "type": "string",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/event cap-placement (Legacy)",
                "desc": "Lock a strict budget limit on an event\u2019s placement payouts (Unmanaged).",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    },
                    {
                        "name": "total_budget",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/event placement (Legacy)",
                "desc": "Award an arbitrary winner\u2019s placement payout. Use `/event award` instead.",
                "access": "admin",
                "params": [
                    {
                        "name": "event_id",
                        "type": "autocomplete",
                        "required": true
                    },
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "placement",
                        "type": "string",
                        "required": true
                    },
                    {
                        "name": "total_ep_value",
                        "type": "number",
                        "required": true
                    }
                ]
            }
        ]
    },
    {
        "category": "Giveaways & Raffles",
        "emoji": "\ud83c\udf81",
        "id": "giveaways",
        "features": [
            {
                "name": "Proof Thread System",
                "type": "passive",
                "desc": "Raffles with requirements auto-create proof threads. Entrants are added and must screenshot proof of completion."
            },
            {
                "name": "Giveaway Notification Ping",
                "type": "passive",
                "desc": "When a raffle is deployed, the Giveaway Notification role is automatically pinged above the embed."
            },
            {
                "name": "Multiline Requirements",
                "type": "passive",
                "desc": "Requirements/mechanics support multiline text via a Modal popup, allowing formatted instructions with line breaks."
            },
            {
                "name": "Scheduled Booster Weekly Raffle",
                "type": "passive",
                "desc": "Runs every Sunday at 8:00 AM PHT. If fewer than 25 boosters, excess slots are fairly distributed prioritizing those with fewest excess wins this month. Winner count is configurable."
            },
            {
                "name": "Auto-Draw with Crash Recovery",
                "type": "passive",
                "desc": "Timed raffles auto-draw when expired. If the bot was offline when a raffle expired, it draws immediately on restart."
            },
            {
                "name": "Smart Reroll Engine",
                "type": "passive",
                "desc": "Reroll mechanics support full-raffle redrawing or single-user replacements, ensuring existing winners are mathematically excluded from snatching the replaced slot."
            }
        ],
        "commands": [
            {
                "syntax": "Step 1: /event raffle create",
                "desc": "Create a raffle. A two-step flow collects core params, then opens a Modal for multiline requirements. Pings Giveaway Notification role.",
                "access": "admin",
                "params": [
                    {
                        "name": "title",
                        "type": "string",
                        "required": true
                    },
                    {
                        "name": "prize",
                        "type": "string",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": true
                    },
                    {
                        "name": "winners",
                        "type": "number",
                        "required": false
                    },
                    {
                        "name": "duration_minutes",
                        "type": "number",
                        "required": false
                    },
                    {
                        "name": "hosted_by",
                        "type": "@mention",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "Step 2: /event raffle set_timer",
                "desc": "Set or update the auto-draw end time on an active raffle.",
                "access": "admin",
                "params": [
                    {
                        "name": "raffle_id",
                        "type": "number",
                        "required": true
                    },
                    {
                        "name": "duration_minutes",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "Step 3: /event raffle draw",
                "desc": "Manually draw winners for an active raffle.",
                "access": "admin",
                "params": [
                    {
                        "name": "raffle_id",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "Step 4a: /event raffle reroll",
                "desc": "Reroll an ended raffle. Disqualify someone or reroll all.",
                "access": "admin",
                "params": [
                    {
                        "name": "raffle_id",
                        "type": "number",
                        "required": true
                    },
                    {
                        "name": "disqualified_winner",
                        "type": "@mention",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "Step 4b: /event raffle export_winners",
                "desc": "Export a drawn raffle's winners as a CSV file.",
                "access": "admin",
                "params": [
                    {
                        "name": "raffle_id",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "Step 1: /booster-raffle-status",
                "desc": "Diagnostic dashboard for the auto booster raffle: channel/role config, booster count, this-week execution status, and next scheduled time with Unix timestamps.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "Step 2: /force-booster-raffle",
                "desc": "Forcefully execute the weekly booster Diamond Raffle. Pings Server Booster role. Counts as this week's raffle (auto raffle won't re-run).",
                "access": "admin",
                "params": [
                    {
                        "name": "ignore_7day_rule",
                        "type": "boolean",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "Step 3: /booster-raffle-reroll-msl",
                "desc": "Retroactively exclude MSL members from the latest draw and reallocate slots to valid non-MSL boosters. Edits original announcement.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "Step 4: /booster-raffle-export",
                "desc": "Export the latest booster raffle winners to separated MSL and Non-MSL CSVs.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/raffles",
                "desc": "List all active raffles with participant count and end time.",
                "access": "general",
                "params": []
            },
            {
                "syntax": "/event raffle cancel (Maintenance)",
                "desc": "Cancel an active raffle and update its embed.",
                "access": "admin",
                "params": [
                    {
                        "name": "raffle_id",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/event raffle sync_legacy (Maintenance)",
                "desc": "Retroactively locate and cache message IDs for previously drawn raffle announcements.",
                "access": "admin",
                "params": [
                    {
                        "name": "raffle_id",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/event raffle force_sync (Maintenance)",
                "desc": "Overwrite legacy announcement messages to match current DB winners.",
                "access": "admin",
                "params": [
                    {
                        "name": "raffle_id",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/booster-raffle-surgeon (Maintenance)",
                "desc": "Emergency fix to purge test rounds and securely map valid winners to a specific target announcement message.",
                "access": "admin",
                "params": [
                    {
                        "name": "message_link_or_id",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/booster-raffle-diagnose (Maintenance)",
                "desc": "Dry-run diagnostic: traces every step the surgeon would take without modifying database records.",
                "access": "admin",
                "params": [
                    {
                        "name": "message_link_or_id",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/booster-raffle-delete (Maintenance)",
                "desc": "Safely purge a specifically linked test draw and explicitly re-enable the auto-raffle.",
                "access": "admin",
                "params": [
                    {
                        "name": "message_link_or_id",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/booster-raffle-purge-week (Maintenance)",
                "desc": "Emergency clear: Wipes ALL raffle records for the current calendar week.",
                "access": "admin",
                "params": []
            }
        ]
    },
    {
        "category": "Community & Social",
        "emoji": "\ud83c\udfad",
        "id": "social",
        "features": [
            {
                "name": "Anonymous Sticky Panel",
                "type": "passive",
                "desc": "The anon message panel auto-reposts to the bottom of the channel every 10 minutes if it\u2019s no longer the latest message."
            },
            {
                "name": "Threaded Anonymous Replies",
                "type": "passive",
                "desc": "Anonymous replies are routed into Discord inline threads to prevent identity meta-tracking."
            },
            {
                "name": "Counting Game Persistence",
                "type": "passive",
                "desc": "The counting channel survives bot restarts via retroactive sync, processing any messages sent while offline."
            }
        ],
        "commands": [
            {
                "syntax": "Step 1: /anon deploy",
                "desc": "Deploy the sticky anonymous messaging panel.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "Step 1: /confessions deploy",
                "desc": "Deploy the anonymous confessions board panel.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "Step 1: /notification deploy",
                "desc": "Deploy the notification role self-assignment panel (6 toggle buttons).",
                "access": "admin",
                "params": [
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "Step 2: /anon sync",
                "desc": "Force re-number all anonymous messages sequentially (fixes gaps from deletions).",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "Step 2: /confessions sync",
                "desc": "Force re-number all confessions sequentially (fixes gaps from deletions).",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/thank",
                "desc": "Thank someone for their help. Awards them +10 XP. 12h cooldown per sender, 5 unique thanks/day cap.",
                "access": "general",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/bind-badges",
                "desc": "Bind dynamic badge names to Discord roles (one-time admin setup).",
                "access": "admin",
                "params": []
            }
        ]
    },
    {
        "category": "Quiz & Quests",
        "emoji": "\ud83e\udde0",
        "id": "quiz",
        "features": [
            {
                "name": "Automated Quiz Sessions",
                "type": "passive",
                "desc": "10-round MLBB lore quiz runs automatically at Noon and 8 PM PHT daily. Channel locks/unlocks automatically."
            },
            {
                "name": "Pre-Quiz Notification",
                "type": "passive",
                "desc": "The Quiz Notification role is pinged 5 minutes before each quiz session starts."
            },
            {
                "name": "Daily Quests",
                "type": "passive",
                "desc": "Users receive 3 daily quests (1 Common, 1 Uncommon, 1 weighted random) that track messages, reactions, voice minutes, and more."
            }
        ],
        "commands": [
            {
                "syntax": "Step 1: /quiz reload",
                "desc": "Reload quiz questions from CSV into memory.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "Step 2: /quiz status",
                "desc": "Check quiz system status (questions loaded, session state, schedule).",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "Step 3: /quiz start",
                "desc": "Manually start a quiz session now.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "Step 4: /quiz stop",
                "desc": "Force-stop a running quiz session and re-lock the channel.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/quiz-leaderboard",
                "desc": "View all-time quiz EP earnings leaderboard.",
                "access": "general",
                "params": []
            },
            {
                "syntax": "/quests",
                "desc": "View your 3 daily quests with progress bars. Auto-generates new quests if none exist for today.",
                "access": "general",
                "params": []
            },
            {
                "syntax": "/manage-quests",
                "desc": "Open the admin quest catalog. Create, edit, and delete quest templates via interactive UI.",
                "access": "admin",
                "params": []
            }
        ]
    },
    {
        "category": "Verification & MSL",
        "emoji": "\u2705",
        "id": "verification",
        "features": [
            {
                "name": "MLBB Account Linking",
                "type": "passive",
                "desc": "Users verify by submitting their MLBB UID and Server ID. Verified users gain access to XP and EP systems."
            },
            {
                "name": "MSL Google Sheets Sync",
                "type": "passive",
                "desc": "The bot syncs with a Google Spreadsheet to validate MSL membership and auto-assign the MSL role."
            }
        ],
        "commands": [
            {
                "syntax": "Step 1: /msl setup",
                "desc": "Configure the MSL spreadsheet URL and verified role.",
                "access": "admin",
                "params": [
                    {
                        "name": "spreadsheet_url",
                        "type": "url",
                        "required": true
                    },
                    {
                        "name": "role",
                        "type": "@role",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "Step 2: /verify deploy",
                "desc": "Post the verification panel in a channel.",
                "access": "admin",
                "params": [
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "Step 3: /verify update",
                "desc": "Edit a user\u2019s MLBB verification info.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "mlbb_uid",
                        "type": "number",
                        "required": true
                    },
                    {
                        "name": "server_id",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "Step 4: /verify remove",
                "desc": "Remove a user\u2019s verification status.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/verify whois",
                "desc": "Look up a Discord user by their MLBB UID.",
                "access": "admin",
                "params": [
                    {
                        "name": "mlbb_uid",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/msl refresh",
                "desc": "Force refresh the MSL member cache from Google Sheets.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/msl check",
                "desc": "Check if a verified user is an MSL member.",
                "access": "admin",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    }
                ]
            }
        ]
    },
    {
        "category": "Embed & Ticket Systems",
        "emoji": "\ud83d\udce8",
        "id": "embeds",
        "features": [
            {
                "name": "Scheduled Embeds",
                "type": "passive",
                "desc": "Embeds can be scheduled for future delivery with automatic timezone handling and crash recovery."
            },
            {
                "name": "Ticket Rating System",
                "type": "passive",
                "desc": "When tickets are closed, users can rate their support experience. Ratings feed into ticket stats."
            }
        ],
        "commands": [
            {
                "syntax": "/embed send",
                "desc": "Send an embed from a Discohook backup link to any channel.",
                "access": "admin",
                "params": [
                    {
                        "name": "link",
                        "type": "url",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": false
                    },
                    {
                        "name": "schedule",
                        "type": "string",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/embed edit",
                "desc": "Edit an existing message using a new Discohook link.",
                "access": "admin",
                "params": [
                    {
                        "name": "message_id",
                        "type": "number",
                        "required": true
                    },
                    {
                        "name": "link",
                        "type": "url",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/embed download",
                "desc": "Generate a Discohook link from an existing Discord message.",
                "access": "admin",
                "params": [
                    {
                        "name": "message_id",
                        "type": "number",
                        "required": true
                    },
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/embed manage",
                "desc": "View, preview, and manage your scheduled embeds.",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/embed logs",
                "desc": "Set the channel for scheduled embed delivery logs.",
                "access": "admin",
                "params": [
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/ticket deploy",
                "desc": "Post the support ticket panel in a channel.",
                "access": "admin",
                "params": [
                    {
                        "name": "channel",
                        "type": "#channel",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/ticket config-category",
                "desc": "Set the channel category for new ticket channels.",
                "access": "admin",
                "params": [
                    {
                        "name": "category",
                        "type": "category",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/ticket config-roles",
                "desc": "Map support roles to ticket categories.",
                "access": "admin",
                "params": [
                    {
                        "name": "category",
                        "type": "string",
                        "required": true
                    },
                    {
                        "name": "role",
                        "type": "@role",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/ticket test",
                "desc": "Toggle test mode for a ticket (marks as non-counted).",
                "access": "admin",
                "params": []
            },
            {
                "syntax": "/ticket stats",
                "desc": "View ticket rating statistics.",
                "access": "admin",
                "params": []
            }
        ]
    },
    {
        "category": "Voice & Pomodoro",
        "emoji": "\ud83c\udfae",
        "id": "utilities",
        "features": [
            {
                "name": "Dynamic Temporary VCs",
                "type": "passive",
                "desc": "When joining the configured auto-create voice channel, a temporary personalized VC is created and self-destructs when empty."
            },
            {
                "name": "Pomodoro Cycles",
                "type": "passive",
                "desc": "25 min work \u2192 5 min break (x4), then 15 min long break. VC renames and participants are pinged at each transition."
            }
        ],
        "commands": [
            {
                "syntax": "/voice setup",
                "desc": "Configure a voice channel to auto-create temporary VCs when joined.",
                "access": "admin",
                "params": [
                    {
                        "name": "channel",
                        "type": "voice",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/voice remove",
                "desc": "Remove auto-create from a voice channel.",
                "access": "admin",
                "params": [
                    {
                        "name": "channel",
                        "type": "voice",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/pomodoro start",
                "desc": "Start a Pomodoro session in your current temp VC. Optionally add up to 4 participants.",
                "access": "general",
                "params": [
                    {
                        "name": "user1\u2013user4",
                        "type": "@mention",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/pomodoro add",
                "desc": "Add a user to the active Pomodoro session (creator only).",
                "access": "general",
                "params": [
                    {
                        "name": "user",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/pomodoro leave",
                "desc": "Leave the Pomodoro session (stop getting pinged).",
                "access": "general",
                "params": []
            },
            {
                "syntax": "/pomodoro stop",
                "desc": "End the Pomodoro session for everyone (creator only).",
                "access": "general",
                "params": []
            }
        ]
    },
    {
        "category": "Booster Perks",
        "emoji": "\ud83c\udf1f",
        "id": "booster_tier",
        "features": [
            {
                "name": "Tiered Boost Promotions",
                "type": "passive",
                "desc": "Boosters progress through tiers based on consecutive boost duration, unlocking color roles, emblems, and multipliers."
            },
            {
                "name": "Weekly Spotlight",
                "type": "passive",
                "desc": "A random booster is spotlighted weekly with a public announcement in the boost channel."
            }
        ],
        "commands": [
            {
                "syntax": "/booster color",
                "desc": "Choose an exclusive booster color role from the configured palette.",
                "access": "booster",
                "params": []
            },
            {
                "syntax": "/booster emblem",
                "desc": "Choose an exclusive emblem cosmetic (Tier 2+ only).",
                "access": "booster",
                "params": []
            },
            {
                "syntax": "/booster perks",
                "desc": "View your booster tier progress, perks, and multipliers.",
                "access": "booster",
                "params": []
            },
            {
                "syntax": "/booster list",
                "desc": "List all active server boosters with their tier and duration.",
                "access": "admin",
                "params": []
            }
        ]
    },
    {
        "category": "Debug & Testing",
        "emoji": "\ud83d\udd27",
        "id": "test",
        "features": [],
        "commands": [
            {
                "syntax": "/test add-xp",
                "desc": "Force add XP to a user.",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "amount",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/test add-ep",
                "desc": "Force add Event Points to a user.",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "amount",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/test add-tokens",
                "desc": "Force add Economy Tokens (triggers Mogul badge evaluation).",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "amount",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/test set-streak",
                "desc": "Force set daily activity streak (triggers Twilight Pilgrim badge).",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "days",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/test set-events",
                "desc": "Force set consecutive events attended (triggers Convivialist badge).",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "events",
                        "type": "number",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/test reset-user",
                "desc": "Reset ALL economy/tracking stats and strip badge roles for a user.",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/test xp-debug",
                "desc": "Check internal XP state variables (loop status, pending XP, cooldown).",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/test xp-dryrun",
                "desc": "Simulate an XP message to diagnose why XP isn\u2019t being awarded.",
                "access": "admin",
                "params": [
                    {
                        "name": "test_message_content",
                        "type": "string",
                        "required": false
                    }
                ]
            },
            {
                "syntax": "/test check-db",
                "desc": "Raw SQL dump of a user\u2019s row in the users table.",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/testrole",
                "desc": "Debug test role assignment with detailed permission checks.",
                "access": "admin",
                "params": [
                    {
                        "name": "member",
                        "type": "@mention",
                        "required": true
                    },
                    {
                        "name": "role",
                        "type": "@role",
                        "required": true
                    }
                ]
            }
        ]
    },
    {
        "category": "Referral System",
        "emoji": "\ud83d\udd17",
        "id": "referrals",
        "features": [
            {
                "name": "Deterministic Code Generation",
                "type": "passive",
                "desc": "Each user gets a unique, permanent referral code derived from their Discord ID (base-36 encoded). The same user always gets the same code."
            },
            {
                "name": "Verification Integration",
                "type": "passive",
                "desc": "The verification modal includes an optional Referral Code field. Valid codes are silently linked on successful verification without blocking the process."
            },
            {
                "name": "Weekly Stats Reset",
                "type": "passive",
                "desc": "Every Sunday at midnight PHT, current week referral counts shift to previous week and reset. A settings flag prevents double-runs."
            },
            {
                "name": "New Member Eligibility",
                "type": "passive",
                "desc": "Only members who joined within the last 30 days can use a referral code. Self-referrals and duplicate uses are blocked."
            }
        ],
        "commands": [
            {
                "syntax": "/referral view",
                "desc": "View your unique referral code and tracking stats (total, this week, last week). Share your code with new members during verification.",
                "access": "general",
                "params": []
            },
            {
                "syntax": "/referral link",
                "desc": "Link a referral code if you missed it during verification. Only works for members who joined within the last 30 days.",
                "access": "general",
                "params": [
                    {
                        "name": "code",
                        "type": "string",
                        "required": true
                    }
                ]
            },
            {
                "syntax": "/referral leaderboard",
                "desc": "View the top 10 referrers ranked by all-time total and current week counts in a single embed.",
                "access": "general",
                "params": []
            },
            {
                "syntax": "/referral previous",
                "desc": "Admin-only command showing all users who had referrals last week, sorted by count descending.",
                "access": "admin",
                "params": []
            }
        ]
    }
];
