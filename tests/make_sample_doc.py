"""Generates a deterministic ~10k-word casual-tone product guide as Markdown + PDF.

Planted features the tests rely on:
  * a Pricing table + paragraph (Starter $29 / Pro $79 / Enterprise $249)
  * the Pro price repeated in the FAQ and in a summary far away from Pricing
  * exactly one paragraph about the legacy v1 API (and a v2 section that must survive)
  * contractions/slang throughout, links, code blocks, lists
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from docupdate.pdf_io import markdown_to_pdf  # noqa: E402

random.seed(7)

FEATURES = ["Dashboards", "Alerts", "Data Connectors", "Query Builder", "Scheduled Reports", "Anomaly Detection",
            "Team Workspaces", "Audit Logs", "Embedded Analytics", "Forecasting", "Data Catalog", "Usage Insights",
            "Row-Level Security", "Notebook Mode", "Metric Layer", "Mobile App"]
OPENERS = ["Honestly, {f} is one of those things you'll wonder how you lived without.",
           "So here's the deal with {f}: it's pretty awesome once you get the hang of it.",
           "{f} is where a lot of our users end up spending most of their day, and that's no accident.",
           "Let's be real, nobody gets excited about {f} until they've actually tried it."]
MIDDLES = ["You can set it up in about {n} minutes, and you don't need to write any code to get going.",
           "Most teams we've talked to say it saved them roughly {n} hours a week, which is kinda wild.",
           "It's built on the same engine as the rest of Nimbus, so it's fast even with {n} million rows.",
           "We've shipped {n} improvements to it this year alone, mostly based on stuff you told us.",
           "If you're on a bigger team, you'll love that up to {n} people can work on it at the same time.",
           "It plays nicely with your existing tools, so you won't have to rip anything out."]
TIPS = ["Pro tip: hit the little star icon to pin it to your sidebar so it's always one click away.",
        "Heads up: changes are saved automatically, so don't worry about losing your work.",
        "Quick note: admins can lock this down per workspace if you need tighter control.",
        "If something looks off, just ping support and we'll sort it out super quick.",
        "You can also kick this off from the CLI if that's more your thing."]
CLOSERS = ["Bottom line: give it a spin and see what you think.",
           "That's pretty much it, it's really not complicated.",
           "Anyway, we think you're gonna like it a lot.",
           "Try it out and let us know how it goes, we're all ears."]


def para(f: str) -> str:
    s = [random.choice(OPENERS).format(f=f)]
    s += [m.format(n=random.randint(3, 40)) for m in random.sample(MIDDLES, 3)]
    s += [random.choice(TIPS), random.choice(CLOSERS)]
    return " ".join(s)


def feature_section(f: str) -> str:
    slug = f.lower().replace(" ", "-")
    out = [f"### {f}", para(f), para(f)]
    out.append(f"- Works with every plan, though some limits depend on your tier\n- Full docs live at https://docs.nimbus.example/{slug}\n"
               f"- Keyboard shortcut: press `{f[0].lower()}` twice to jump straight here")
    out += [para(f), para(f), para(f), para(f)]
    return "\n\n".join(out)


INTRO = """# Nimbus Analytics Product Guide

Hey there, and welcome to Nimbus! We're super glad you're here. This guide walks you through pretty much everything you'll need to know, from setting up your first dashboard to wrangling the API. It's long, so grab a coffee and feel free to skip around.

Nimbus is a cloud analytics platform that lets you hook up your data, poke at it, and share what you find with your team. We've been building it since 2019 and today over 4,000 companies use it every day. Our whole thing is making analytics not suck, so if anything in here is confusing, that's on us, not you.

## Getting Started

Getting started is dead simple. Sign up at https://nimbus.example/signup, verify your email, and you're in. You don't need a credit card for the 14-day trial, and you can invite up to 5 teammates right away.

1. Create a workspace and give it a name your team will recognize
2. Connect your first data source (Postgres, Snowflake, BigQuery and a bunch more)
3. Build a dashboard from one of our 30 templates
4. Share it with your team and grab a high five

Once you've done that, you've basically got the hang of it. The rest of this guide goes deeper into each piece."""

PRICING = """## Pricing

We've tried to keep pricing dead simple, no hidden fees, no weird surprises. Every plan is billed monthly per workspace, and you can switch plans whenever you want.

| Plan | Price per month | Included seats | Data rows |
| --- | --- | --- | --- |
| Starter | $29 | 3 | 1 million |
| Pro | $79 | 10 | 50 million |
| Enterprise | $249 | Unlimited | Unlimited |

Starter's great if you're just kicking the tires, Pro is what most growing teams go for, and Enterprise is for folks who need SSO, audit logs and a dedicated success manager. Extra seats on Starter and Pro are $8 each per month. If you pay yearly you get 2 months free, which is a pretty sweet deal.

Nonprofits and schools get 50% off any plan, just shoot us an email at billing@nimbus.example and we'll hook you up."""

API = """## API

Nimbus has a full REST API so you can automate pretty much anything you can do in the app. You'll need an API key, which you can grab from Settings, then Developer.

### API v2

The v2 API is the current one and it's what you should be using. It's JSON over HTTPS, it supports pagination with cursors, and it's rate limited to 600 requests per minute per workspace. Full reference lives at https://docs.nimbus.example/api/v2.

```
curl -H "Authorization: Bearer $NIMBUS_KEY" https://api.nimbus.example/v2/dashboards
```

### Legacy API

The legacy v1 API is still hanging around for older integrations, but honestly you shouldn't build anything new on it. It uses XML responses, doesn't support pagination, and it's capped at 60 requests per minute. We're planning to switch it off sometime in 2027, so if you're still on v1 you'll want to migrate to v2 soon. Our migration guide is at https://docs.nimbus.example/api/migrate.

### Webhooks

Webhooks let Nimbus ping your systems when stuff happens, like an alert firing or a report finishing. You can set up to 20 webhook endpoints per workspace, and we'll retry failed deliveries up to 5 times with exponential backoff."""

SECURITY = """## Security and Compliance

We take security super seriously, it's kinda our whole job. All data is encrypted at rest with AES-256 and in transit with TLS 1.2 or higher. We're SOC 2 Type II certified and we do a third-party pen test every year.

Enterprise customers get SSO through SAML or OIDC, SCIM provisioning, and audit logs that are kept for 365 days. If you need a custom data processing agreement, just ask and our legal team will get it sorted."""

FAQ = """## FAQ

**Can I change plans later?** Yep, you can upgrade or downgrade any time from the Billing page and we'll prorate the difference.

**How much does the Pro plan cost?** Pro is $79 per month per workspace and comes with 10 seats, which is plenty for most teams.

**Do you offer a free plan?** Not exactly, but the 14-day trial gives you everything in Pro so you can really dig in.

**What happens to my data if I cancel?** We keep it for 30 days in case you change your mind, then it's deleted for good.

**Is there an on-prem version?** Nope, Nimbus is cloud only for now, but Enterprise customers can pick their data region.

**Who do I talk to about a custom contract?** Just email sales@nimbus.example and someone will get back to you within 1 business day."""

SUMMARY = """## Summary

That's the tour! To recap: start on the trial, connect your data, and most teams land on Pro at $79 a month. If you've got questions we didn't cover, our support team is around 24/7 and they're honestly lovely. Thanks for picking Nimbus, we can't wait to see what you build."""


def build() -> str:
    parts = [INTRO, "## Features\n\nHere's a rundown of the main features. Each one gets its own section so you can jump to whatever you care about."]
    parts += [feature_section(f) for f in FEATURES[:8]]
    parts.append(PRICING)
    parts += [feature_section(f) for f in FEATURES[8:]]
    parts += [API, SECURITY, FAQ, SUMMARY]
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
    os.makedirs(here, exist_ok=True)
    md = build()
    open(os.path.join(here, "nimbus_guide.md"), "w").write(md)
    markdown_to_pdf(md, os.path.join(here, "nimbus_guide.pdf"), header="Nimbus Analytics - Product Guide v4.2")
    print(f"{len(md.split())} words -> samples/nimbus_guide.md, samples/nimbus_guide.pdf")
