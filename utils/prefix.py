async def getprefix(bot, message):
    """
    Utility function to get the bot prefix.
    """
    if not message.guild:
        return ";"

    check = await bot.db.fetchrow(
        """
        SELECT * FROM 
        selfprefix WHERE 
        user_id = $1
        """, 
        message.author.id
    )
    if check:
        selfprefix = check["prefix"]

    res = await bot.db.fetchrow(
        """
        SELECT * FROM 
        prefix WHERE 
        guild_id = $1
        """, 
        message.guild.id
    )
    if res:
        guildprefix = res["prefix"]
    else:
        guildprefix = ";"

    if not check and res:
        selfprefix = res["prefix"]
    elif not check and not res:
        selfprefix = ";"

    return guildprefix, selfprefix 