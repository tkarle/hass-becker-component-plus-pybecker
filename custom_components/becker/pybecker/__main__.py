import argparse
import asyncio

from .becker import Becker


async def main():
    """Main function"""
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--channel')
    parser.add_argument(
        '-a',
        '--action',
        choices=['UP', 'UP2', 'DOWN', 'DOWN2', 'HALT', 'TRAIN'],
        help='Command to execute (UP, DOWN, HALT, TRAIN)',
    )
    parser.add_argument('-d', '--device', help='Device to use for connectivity')
    parser.add_argument('-f', '--file', help='Database file')
    parser.add_argument(
        '-l',
        '--log',
        type = int,
        help='Logs received commands (only UP, DOWN, HALT) for a certain time (in seconds)'
    )
    args = parser.parse_args()

    if (args.channel is None) != (args.action is None):
        parser.error('both --channel and --action are required')

    if args.log is None:
        callback = None
    else:
        commands = {"1": "HALT", "2": "UP", "4": "DOWN"}

        def callback(packet):
            print(
                "Received packet: "
                f"unit_id: {packet.group('unit_id').decode()}, "
                f"channel: {packet.group('channel').decode()}, "
                f"command: {commands.get(packet.group('command').decode(), 'UNKNOWN')}, "
                f"argument: {packet.group('argument').decode()}"
            )

    client = Becker(device_name=args.device, db_filename=args.file, callback=callback)

    if args.action == "UP":
        await client.move_up(args.channel)
    elif args.action == "UP2":
        await client.move_up_intermediate(args.channel)
    elif args.action == "HALT":
        await client.stop(args.channel)
    elif args.action == "DOWN":
        await client.move_down(args.channel)
    elif args.action == "DOWN2":
        await client.move_down_intermediate(args.channel)
    elif args.action == "TRAIN":
        await client.pair(args.channel)

    # wait for log
    await asyncio.sleep(args.log or 0)

    # graceful shutdown
    client.close()

if __name__ == '__main__':
    import sys

    # to avoid crashing on exit when running on windows
    if sys.version_info[0] == 3 and sys.version_info[1] >= 8 and sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())
