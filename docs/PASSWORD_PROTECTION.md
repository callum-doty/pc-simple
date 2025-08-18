# Password Protection Setup Guide

This document explains how to configure and use the password protection feature for the Document Catalog application.

## Overview

The application now includes a comprehensive password protection system that requires users to authenticate before accessing any part of the web application. This provides a simple but secure way to control access to your document catalog.

## Features

- **Session-based authentication** with secure cookies
- **Beautiful login page** with modern UI design
- **Rate limiting** on login attempts (10 attempts per minute)
- **Session timeout** (configurable, default 24 hours)
- **Remember me** functionality for extended sessions
- **Automatic redirects** to intended pages after login
- **Secure logout** with session cleanup

## Configuration

### Environment Variables

Add these settings to your `.env` file or environment:

```bash
# App-wide password protection
APP_PASSWORD=your-secure-password-here
REQUIRE_APP_AUTH=true
SESSION_TIMEOUT_HOURS=24
SESSION_SECRET_KEY=your-session-secret-key-change-in-production

# Optional: Disable for development
# REQUIRE_APP_AUTH=false
```

### Configuration Options

| Setting                 | Default                                      | Description                                 |
| ----------------------- | -------------------------------------------- | ------------------------------------------- |
| `APP_PASSWORD`          | `"secure123"`                                | Password required to access the application |
| `REQUIRE_APP_AUTH`      | `true`                                       | Enable/disable password protection          |
| `SESSION_TIMEOUT_HOURS` | `24`                                         | Session expiration time in hours            |
| `SESSION_SECRET_KEY`    | `"your-session-secret-change-in-production"` | Secret key for session encryption           |

## Security Features

### Password Protection

- Simple password-based authentication
- Configurable via environment variables
- Rate limiting prevents brute force attacks

### Session Management

- Secure session cookies with HttpOnly and Secure flags
- Configurable session timeout
- Automatic session cleanup on logout
- Session validation on each request

### Security Headers

- Content Security Policy (CSP)
- X-Frame-Options: SAMEORIGIN
- X-Content-Type-Options: nosniff
- X-XSS-Protection: 1; mode=block
- Referrer-Policy: strict-origin-when-cross-origin

## Usage

### First Time Setup

1. Set your password in the environment:

   ```bash
   export APP_PASSWORD="your-secure-password"
   export REQUIRE_APP_AUTH=true
   ```

2. Start the application:

   ```bash
   python main.py
   ```

3. Navigate to your application URL - you'll be redirected to the login page

### Login Process

1. **Access any URL** - you'll be redirected to `/login`
2. **Enter your password** - the password you configured in `APP_PASSWORD`
3. **Optional: Check "Remember me"** - extends session duration
4. **Click "Access Application"** - you'll be redirected to your intended page

### Logout

- Click the **"Logout"** button in the navigation bar
- Or navigate directly to `/logout`
- Your session will be destroyed and you'll be redirected to the login page

## Development Mode

For development, you can disable password protection:

```bash
export REQUIRE_APP_AUTH=false
```

This allows unrestricted access to the application during development.

## Production Deployment

### Security Recommendations

1. **Use a strong password**:

   ```bash
   export APP_PASSWORD="a-very-secure-password-with-numbers-123"
   ```

2. **Use a secure session secret**:

   ```bash
   export SESSION_SECRET_KEY="a-long-random-string-for-session-encryption"
   ```

3. **Enable HTTPS** - the session cookies will automatically use the Secure flag

4. **Consider shorter session timeouts** for high-security environments:
   ```bash
   export SESSION_TIMEOUT_HOURS=8
   ```

### Environment Variables for Production

```bash
# Required
APP_PASSWORD=your-production-password
SESSION_SECRET_KEY=your-production-session-secret
REQUIRE_APP_AUTH=true

# Optional
SESSION_TIMEOUT_HOURS=24
ENVIRONMENT=production
```

## Troubleshooting

### Common Issues

1. **"Invalid password" error**

   - Check that `APP_PASSWORD` environment variable is set correctly
   - Ensure no extra spaces or characters in the password

2. **Session expires too quickly**

   - Increase `SESSION_TIMEOUT_HOURS` value
   - Check that session cookies are being saved by the browser

3. **Redirected to login repeatedly**

   - Check browser console for JavaScript errors
   - Verify `SESSION_SECRET_KEY` is set and consistent
   - Clear browser cookies and try again

4. **Rate limiting errors**
   - Wait a few minutes before trying to login again
   - Rate limit is 10 attempts per minute per IP address

### Logs

The application logs authentication events:

- Login attempts (successful and failed)
- Session creation and destruction
- Rate limiting events

Check the application logs for debugging authentication issues.

## API Access

When password protection is enabled, API endpoints also require authentication:

- **Browser sessions**: Automatically authenticated after login
- **Direct API access**: Returns 401 Unauthorized for unauthenticated requests

For programmatic API access, you would need to implement session-based authentication or consider adding API key authentication for specific endpoints.

## Customization

### Login Page Styling

The login page template is located at `templates/login.html` and can be customized:

- Colors and branding
- Logo and messaging
- Additional form fields
- Custom JavaScript functionality

### Session Behavior

Session behavior can be modified in `services/security_service.py`:

- Session validation logic
- Cookie settings
- Timeout handling
- Remember me functionality

## Security Considerations

1. **Password Storage**: The password is compared in plain text. For enhanced security, consider implementing password hashing.

2. **Session Security**: Sessions use secure, HttpOnly cookies with CSRF protection.

3. **Rate Limiting**: Login attempts are rate-limited to prevent brute force attacks.

4. **HTTPS**: Always use HTTPS in production for secure cookie transmission.

5. **Session Timeout**: Configure appropriate timeout values based on your security requirements.

This password protection system provides a good balance of security and usability for controlling access to your document catalog application.
