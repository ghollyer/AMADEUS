result( "\n\nActive application mode:" + SIGetMode() )
result( "\nAccessible items:\n" )
number maxSearch = 1000

for ( number id = 0; id < maxSearch; id++ )
{
string name = SIGetPaletteItemName( id )
if ( name != "" )
result( Format( id, "ID[%4.f]: " ) + name + "\n" )
}