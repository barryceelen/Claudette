# Claudette – Claude AI Assistant for Sublime Text

![Claude Chat View](/screenshot.png?raw=true "Ask Claude")

A [Sublime Text](http://www.sublimetext.com) package that integrates the Anthropic Claude API into your editor.

Type "Ask Claude" in the command palette or find the *Ask Claude* item in the *Tools* menu or in the right-click context menu to ask a question. Any selected text in the current file will be sent along to the Anthropic Claude API. Note that a Claude API key is required.

## Features

- Chat with Claude in multiple chat windows at the same time
- Automatically include selected text as context for your questions
- Choose between different Claude [models](https://docs.anthropic.com/en/docs/about-claude/models)
- Configure custom [system prompts](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/system-prompts) to customize Claude's behavior
- Chat History: Export and import conversations as JSON files

## Available commands

All commands are available via the *Tools > Claudette* menu or via the command palette.

- **Ask Question**  
*claudette\_ask\_question*  
Opens a question input prompt. Submit the prompt with the `enter` key, `shift+enter` for line breaks.

- **Ask Question In New Chat View**  
*claudette\_ask\_new\_question*  
Opens a question input prompt. The conversation will take place in a new window. Useful for having multiple chats in the same window.

- **Clear Chat History**   
*claudette\_clear\_chat\_history*  
Clear the chat history to reduce token usage while keeping previous messages visible in the interface. This prevents resending old messages during new queries.

- **Export Chat History**  
*claudette\_export\_chat\_history*  
Save any Claude chat conversation. Run this command to export the most recently active chat view in the current window to a JSON file.

- **Import Chat History**  
*claudette\_export\_chat\_history*  
Import a chat history JSON file and continue the conversation where it left off.

- **Switch Model**  
*claudette\_select\_model\_panel*  
Claudette chat is powered by Claude 3.5 Sonnet by default, but you can switch between all available Anthropic models.

- **Switch System Prompt**  
*claudette\_select\_system\_message\_panel*  
Give Claude a role by adding a system prompt. Multiple system prompts can be added via the Claudette settings. This command allows you to switch the system prompt that is sent along with a conversation.

## Key Bindings

The Claudette package does not add [key bindings](https://www.sublimetext.com/docs/key_bindings.html) for its commands out of the box. The following example adds a handy keyboard shortcut that opens the "Ask Question" panel. You can add your own keyboard shortcuts via the *Settings > Keybindings* settings menu.

For OSX:

```
[
	{
		"keys": ["super+k", "super+c"],
		"command": "claudette_ask_question",
	}
]
```

For Linux and Windows:

```
[
	{
		"keys": ["ctrl+k", "ctrl+c"],
		"command": "claudette_ask_question",
	}
]
```

## Installation

1. In Sublime Text, add the `https://github.com/barryceelen/Claudette` repository URL via the *Package Control: Add Repository* command
2. Once the repository is added, use the *Package Control: Install Package* command to install the `Claudette` package
2. Get an API key from [Anthropic](https://console.anthropic.com/)
3. Configure API key in *Preferences > Package Settings > Claudette > Settings*

The package is for the most part written by Claude AI itself!
