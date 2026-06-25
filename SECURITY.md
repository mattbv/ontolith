# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately.

**Do not open a public issue.**

Contact: matheus.boni.vicari@gmail.com

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Response Timeline

- Initial response: Within 48 hours
- Status update: Within 7 days
- Fix timeline: Depends on severity

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.x.x   | :white_check_mark: |

## Security Best Practices

When using Ontolith:
- Keep dependencies updated
- Use OIDC for authentication (not API keys in production)
- Review AI-authored assertions before accepting
- Configure appropriate policy thresholds
- Enable audit logging in production
