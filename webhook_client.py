#!/usr/bin/env python3
import argparse
import requests
import json
import sys
import time

# Server configuration
SERVER_URL = "http://localhost:5001/webhook"
TIMEOUT = 30  # 30 seconds timeout for operations

def send_webhook(action, **kwargs):
    """Send a webhook to the server with the specified action and parameters."""
    data = {
        'action': action,
        **kwargs
    }

    print(f"Sending webhook to {SERVER_URL}")
    print(f"Action: {action}")
    print(f"Parameters: {kwargs}")

    try:
        # Add a timeout to prevent hanging
        response = requests.post(SERVER_URL, json=data, timeout=TIMEOUT)

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            print("Command sent successfully")
            return True
        else:
            print(f"Error: Server returned status code {response.status_code}")
            return False

    except requests.exceptions.Timeout:
        print(f"Error: Request timed out after {TIMEOUT} seconds")
        print("The server might be busy or the operation is taking too long.")
        print("You can try again later or check the server logs for more information.")
        return False
    except requests.exceptions.ConnectionError:
        print("Error: Could not connect to the server")
        print("Make sure the server is running and accessible at", SERVER_URL)
        return False
    except requests.exceptions.RequestException as e:
        print(f"Error sending webhook: {str(e)}")
        return False
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return False

def main():
    parser = argparse.ArgumentParser(description='MEXC Playwright Webhook Client')
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Navigate command
    navigate_parser = subparsers.add_parser('navigate', help='Navigate to a URL')
    navigate_parser.add_argument('--url', required=True, help='URL to navigate to')

    # Open long position command
    open_long_parser = subparsers.add_parser('open_long', help='Open a long position')
    open_long_parser.add_argument('--symbol', default='SOL_USDT', help='Trading pair symbol (default: SOL_USDT)')
    open_long_parser.add_argument('--leverage', type=int, default=1, help='Leverage to use (default: 1)')
    open_long_parser.add_argument('--quantity', type=int, default=100, help='Quantity percentage (default: 100)')

    # Open short position command
    open_short_parser = subparsers.add_parser('open_short', help='Open a short position')
    open_short_parser.add_argument('--symbol', default='SOL_USDT', help='Trading pair symbol (default: SOL_USDT)')
    open_short_parser.add_argument('--leverage', type=int, default=1, help='Leverage to use (default: 1)')
    open_short_parser.add_argument('--quantity', type=int, default=100, help='Quantity percentage (default: 100)')

    # Close long position command
    close_long_parser = subparsers.add_parser('close_long', help='Close a long position')
    close_long_parser.add_argument('--symbol', default='SOL_USDT', help='Trading pair symbol (default: SOL_USDT)')
    close_long_parser.add_argument('--quantity', type=int, default=100, help='Quantity percentage (default: 100)')

    # Close short position command
    close_short_parser = subparsers.add_parser('close_short', help='Close a short position')
    close_short_parser.add_argument('--symbol', default='SOL_USDT', help='Trading pair symbol (default: SOL_USDT)')
    close_short_parser.add_argument('--quantity', type=int, default=100, help='Quantity percentage (default: 100)')

    # Close position command (legacy)
    close_position_parser = subparsers.add_parser('close_position', help='Close a position (legacy)')
    close_position_parser.add_argument('--symbol', default='SOL_USDT', help='Trading pair symbol (default: SOL_USDT)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'navigate':
        send_webhook('navigate', url=args.url)
    elif args.command == 'open_long':
        send_webhook('open_long', symbol=args.symbol, leverage=args.leverage, quantity=args.quantity)
    elif args.command == 'open_short':
        send_webhook('open_short', symbol=args.symbol, leverage=args.leverage, quantity=args.quantity)
    elif args.command == 'close_long':
        send_webhook('close_long', symbol=args.symbol, quantity=args.quantity)
    elif args.command == 'close_short':
        send_webhook('close_short', symbol=args.symbol, quantity=args.quantity)
    elif args.command == 'close_position':
        send_webhook('close_position', symbol=args.symbol)

if __name__ == '__main__':
    main()