from setuptools import setup, find_packages

setup(
    name="mexc-playwright",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "requests>=2.31.0",
        "flask>=2.3.3",
        "playwright>=1.40.0",
        "quart>=0.18.4",
        "hypercorn>=0.17.0",
        "argparse>=1.4.0",
        "python-dotenv>=1.0.0",
    ],
    entry_points={
        "console_scripts": [
            "mexc-webhook-server=webhook_server:main",
            "mexc-webhook-client=webhook_client:main",
        ],
    },
    author="Adrian",
    author_email="adrian@example.com",
    description="A webhook-based interface for automating MEXC trading using Playwright",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/adrianmess/mexc-td",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
)