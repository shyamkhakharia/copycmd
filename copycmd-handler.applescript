-- CopyCmd URL scheme handler
-- Receives copycmd://BASE64_ENCODED_COMMAND and copies decoded command to clipboard

on open location this_URL
	-- Strip the "copycmd://" prefix (10 characters, so start at 11)
	set encoded to text 11 thru -1 of this_URL

	-- Decode base64 and copy to clipboard
	try
		set decoded to do shell script "echo " & quoted form of encoded & " | base64 -d"
		set the clipboard to decoded

		-- Show a brief notification
		display notification decoded with title "Copied to clipboard"
	end try
end open location
