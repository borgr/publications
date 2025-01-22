import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
from scipy import interpolate
from scipy.signal import savgol_filter
import math
import os
sns.set_theme(style="white", font="Times New Roman", font_scale=1.5)


# Function to create smooth line
# Function to create smooth line with controllable smoothing

# Function to create smooth line with controllable smoothing

def calculate_angle(x1, y1, x2, y2):
    # Calculate the differences in x and y coordinates
    dx = x2 - x1
    dy = y2 - y1

    # Calculate the angle using arctan2 function
    angle_radians = math.atan2(dy, dx)

    # Convert the angle from radians to degrees
    angle_degrees = math.degrees(angle_radians)

    # Adjust the angle to be within 0 to 360 degrees
    angle_degrees %= 360

    return angle_degrees


def smooth_line(x, y, smoothing_factor=1, num_points=200):
    x_array = np.asarray(x)
    y_array = np.asarray(y)

    # Use Savitzky-Golay filter for smoothing
    window_length = int(len(y_array) / smoothing_factor)
    if window_length % 2 == 0:
        window_length += 1  # Ensure window length is odd
    window_length = max(window_length, 3)  # Ensure window length is at least 3

    y_smooth = savgol_filter(y_array, window_length, 3)

    return x_array, y_smooth


# Smoothing factor (adjust this to control smoothness)
smoothing_factor = 5  # Higher values = more smoothing

# Read the XLS file


df = pd.read_excel(
    os.path.join(os.path.dirname(__file__), 'Contributions_table.xlsx'))
lines = ['Resources', 'The Science of\nDeep Learning', 'Methods', 'Dataset', 'Training',
         'Evaluation', 'Shared-task\effort', 'Language&Cognition', 'Open',
         'Meta-science', 'Enabling Low Budget Research', 'Efficiency', 'NLP']
lines = ['NLP', 'Enabling Low Budget Research', 'The Science of\nDeep Learning', 'Methods',
         'Evaluation', 'Open', 'Language&Cognition',  'Resources'
         ]
df = df.rename(
    columns={'The Science of Deep Learning': 'The Science of\nDeep Learning'})
x = "Time of publish ID"
df = df.dropna(subset=[x])
df = df.sort_values(x)
df_sums = df[lines].fillna(0).cumsum()
df_averages = df[lines].fillna(0).expanding().mean()


# df = read_df()

fig = plt.figure(figsize=(8, 6))
frame_color = "gray"
color_palette = sns.color_palette("husl", n_colors=len(df_averages.columns))
# Plot each column as a line
for i, column in enumerate(lines):
    color = color_palette[i]
    # not too critical, but avoids over reliance on the first paper to discern the beginning trend
    if "ang" in column:  # the first paper + smoothing makes the whole 15 papers at the beginning skewed
        df_averages[column].iloc[0] = 0.75
        df_averages[column].iloc[1] = 0.78
        df_averages[column].iloc[2] = 0.79
        df_averages[column].iloc[3] = 0.8
    elif column != "NLP":
        df_averages[column].iloc[0] = df_averages[column].iloc[0:4].mean()
        df_averages[column].iloc[1] = df_averages[column].iloc[1:5].mean()
        df_averages[column].iloc[2] = df_averages[column].iloc[2:6].mean()

    x_smooth, y_smooth = smooth_line(df[x], df_averages[column],
                                     smoothing_factor=smoothing_factor)
    plt.plot(x_smooth, y_smooth, label=column, color=color)
    sns.despine(left=True, bottom=True)

    # # Get the x value for the i'th position
    # x_pos = np.linspace(df[x].min(), df[x].max(), len(lines))[i]

    # # Get the y value for the i'th column
    # y_pos = df_averages[column][df[x].iloc[i]]

    # Add label above the line
    # Position label at the maximum y value
    y_pos = y_smooth.max()
    # X position where y is maximum
    x_pos = x_smooth[np.argmax(y_smooth)]

    if "Enabling Low Budget Research" == column:
        x_pos -= 10
        column = "Enabling Low Budget Research"

    if y_smooth[3] > y_smooth[-1]:
        starty = max(y_smooth[0:10])
    else:
        starty = min(y_smooth[0:10])

    angle = 30  # pretty but no info, is it better?
    angle = calculate_angle(
        x_smooth[0], starty, x_smooth[-1], y_smooth[-1]) * 50
    if column == 'Resources':
        x_pos += 1
        y_pos -= 0.08
    elif column == 'The Science of\nDeep Learning':
        x_pos -= 6
        y_pos -= 0.17
    elif column == 'Methods':
        x_pos -= 0
        y_pos -= 0
    elif column == 'Evaluation':
        x_pos -= 0
        y_pos += 0.015
    elif column == 'Language&Cognition':
        x_pos += 3.5
        y_pos -= 0.195
    elif column == 'Open':
        x_pos -= 0
        y_pos -= 0.05
    elif column == 'Enabling Low Budget Research':
        x_pos -= 8
        y_pos -= 0.1
    fontweight = "bold"
    xytext = (0, -5)
    fontsize = 14
    if column == "NLP":
        fontsize *= 2
    # Adding shadow text (slightly offset)
    shadow = plt.annotate(column, (x_pos, y_pos), fontweight=fontweight, fontsize=fontsize,
                          textcoords='offset points', ha='center', va='bottom', rotation=angle,
                          color='black',  # Shadow in black
                          xytext=(xytext[0]-0.005, xytext[1]-0.1))
    # add text
    annotation = plt.annotate(column, (x_pos, y_pos), xytext=xytext, fontweight=fontweight, fontsize=fontsize,
                              textcoords='offset points', ha='center', va='bottom', color=color, rotation=angle)


first_years = df["year"].drop_duplicates(keep="first")[1:]
plt.tick_params(axis='both', colors='gray')

plt.ylabel('%Papers on Topic', color=frame_color)
# create a second axis that shares the same x-axis as ax
ax = plt.gca()
plt.xticks([i * 10 for i in range(1, len(df_averages) // 10 + 1)])
plt.tick_params(length=0)
ax2 = ax.twiny()
ax2.tick_params(length=0)
sns.despine(left=True, bottom=True)

# set the x-axis ticks on ax2 to the first occurrence of each year
ax2.set_xticks([a for a in df[x][first_years.index]]+[df[x].max()])
# ax2.set_xticks([a for a in df[x][first_years.index]])
ax2.xaxis.set_ticks_position('bottom')
ax.xaxis.set_ticks_position('top')
ax.xaxis.set_visible(False)
ax.spines['top'].set_position(('outward', 25))

# set the axis label on ax2
# ax2.set_xlabel('Paper count', color=frame_color).set_position(('outward', 11))

ax.set_xlabel('Year', color=frame_color, labelpad=38)
# ax2.set_xlabel('Paper ID', color=frame_color, labelpad=55)

# adjust the position of the top spine
ax2.set_xticklabels(list(first_years) +
                    ["Now"], rotation=45)


# plt.title('Breakdown of works to topics')
# plt.legend()
plt.xticks(rotation=45)
# Change the color of the ticks and tick labels to gray
plt.tick_params(axis='both', colors='gray')
# plt.tight_layout()
plt.savefig("papers.png", bbox_inches="tight", pad_inches=0.1)
plt.savefig("papers.pdf", bbox_inches="tight", pad_inches=0.1)
plt.show()

print()

# TODO
# fix language not to start at 0
# move each separate title
# where is y label?
