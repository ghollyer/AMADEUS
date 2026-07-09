result( "\n\nField values:\n" )
number maxSearch = 1000
for ( number id = 0; id < maxSearch; id++ )
{
number value
if ( SIGetFieldValue(id,value) )
	{
		result( Format( id, "ID[%4.f]: " ) + SIGetPaletteItemName( id) + " = " + value + "\n" )
	}
}