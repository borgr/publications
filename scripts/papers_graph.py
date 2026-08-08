import math
import os
import random

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import umap.umap_ as umap
from matplotlib.patches import PathPatch
from matplotlib.path import Path
from scipy.interpolate import splev, splprep
from scipy.signal import savgol_filter
from scipy.spatial import ConvexHull
from sklearn.manifold import TSNE

sns.set_theme(style="white", font="Times New Roman", font_scale=1.5)
random.seed(0)
np.random.seed(0)


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


def plot_cooccurence(df):

    # Assuming 'df' is your DataFrame with rows as papers and columns as labels
    # Convert the DataFrame to binary (0/1) values
    df_binary = df.applymap(lambda x: 1 if not pd.isna(x) and x > 0 else 0)

    # Calculate the co-occurrence matrix
    co_occurrence_matrix = np.dot(df_binary.T, df_binary)

    # Create a heatmap with hierarchical clustering
    # plt.figure(figsize=(10, 8))
    sns.clustermap(co_occurrence_matrix, annot=True, cmap='Blues', square=True,
                   xticklabels=df.columns, yticklabels=df.columns)

    plt.title('Topic Co-occurrence Matrix')
    plt.xlabel('Topics')
    plt.ylabel('Topics')
    plt.xticks(rotation=90)
    plt.show()


def reduce_tsne(df, n_components=2, perplexity=19, random_state=42):

    # Create a t-SNE object
    tsne = TSNE(n_components=n_components,
                perplexity=perplexity, random_state=random_state)

    # Fit the t-SNE to the data and transform it
    embedding = tsne.fit_transform(df)
    return embedding


def reduce_umap(df, n_neighbors=70, min_dist=1, random_state=42):

    # Create a UMAP object
    reducer = umap.UMAP(n_neighbors=n_neighbors,
                        min_dist=min_dist, random_state=random_state)

    # Fit the UMAP to the data and transform it
    embedding = reducer.fit_transform(df)
    return embedding


efival_col = "Efficient Evaluation"
efival_cols = ["Efficiency", "Evaluation"]
other_effi = "General Efficiency"
collab_col = "Collaborative"
collab_cols = ["Recycling"]
eval_understand_col = "Understanding"
eval_understand_cols = ["Evaluation", "The Science of Deep Learning"]
commun_col = "Accessible"
commun_cols = ["Meta-science", "Recycling",  # "Human-Model Interaction",
               "Enabling Low Budget Research", "Efficiency", "Small Models", "Efficient Pretraining Research"]


def reduce_manually(df, a, b, focus=None):
    def apply(row, direction):
        return df[row].apply(lambda x: x * np.array(direction))
    centroid1 = [1, 0]
    centroid2 = [0, -1]
    centroid3 = [-1, 0]
    centroid4 = [2, -2]
    df = df.copy()
    embedding = apply(collab_col, centroid1) + apply(eval_understand_col,
                                                     centroid2) + apply(commun_col, centroid3)
    # embedding = embedding/embedding.apply(lambda x: max(1, sum(x)))
    importance = 0
    embedding *= importance
    debates_index = df[df["Debating"] == 1].index
    reduct_index = df.index.difference(debates_index)  # ~debates_index
    if focus is None:
        focus = df.columns
    main_reduced = reduce_tsne(df.loc[reduct_index, focus], a, b)
    main_reduced /= np.abs(main_reduced).mean(axis=0)
    reduced = pd.Series(data=list(
        main_reduced) + [centroid4]*len(debates_index), index=list(reduct_index)+list(debates_index)).sort_index()
    # assign the values in main to the
    # reduced = reduce_umap(df, a, b)
    noise = np.random.normal(0, 0.15, (len(reduced), 2))
    # make hum closer
    hum_index = df[df["Human-Model Interaction"] == 1].index
    reduced.iloc[66] = reduced.iloc[66] + 0.8 * \
        (reduced[hum_index].mean() - reduced.iloc[66])
    reduced.iloc[32] = reduced.iloc[32] - np.array([0, 0.2])
    return np.array([red + em + no for red, em, no in zip(reduced, embedding, noise)])


def add_cols(df):
    added = []
    df[other_effi] = df.apply(lambda row: row["Efficiency"] and not any(
        row[["Open", "Recycling", "Evaluation", "Debating"]]), axis=1)
    added.append(other_effi)
    df[collab_col] = df.apply(lambda row: any(row[collab_cols]), axis=1)
    added.append(collab_col)
    df[efival_col] = df.apply(lambda row: all(row[efival_cols]), axis=1)
    added.append(efival_col)
    df[eval_understand_col] = df.apply(
        lambda row: any(row[eval_understand_cols]), axis=1)
    added.append(eval_understand_col)
    df[commun_col] = df.apply(lambda row: any(row[commun_cols]), axis=1)
    added.append(commun_col)
    return added, df


def manual_reweight(df):
    df = df.copy()
    importance = 10
    df[collab_col] *= importance
    df[eval_understand_col] *= importance
    df[commun_col] *= importance
    return df


def lighten_color(hex_color, factor=-0.5):
    """
    Lightens a given hex color by a specified factor.

    Args:
        hex_color (str): The hex color code to lighten.
        factor (float): The factor by which to lighten the color (0-1) or to darken (-1-0)).

    Returns:
        str: The hex code of the lighter color.
    """

    if hex_color.startswith("#"):
        hex_color = hex_color[1:]

    rgb = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

    new_rgb = [max(0, min(int(c + (255 - c) * factor), 255)) for c in rgb]

    return "#" + "".join(f"{c:02x}" for c in new_rgb)


def create_smooth_scaled_hull(points, scale_factor=1.2, smoothing=0.5, num_points=100, color=None, alpha=0.4):
    """
    Create a smooth, scaled hull around a set of points, ensuring all points are enclosed.

    Parameters:
    points: array-like, shape (n, 2)
        The input points to create a hull around
    scale_factor: float
        How much to scale the hull (1.0 = original size)
    smoothing: float
        Controls the smoothness (0 = no smoothing, 1 = max smoothing)
    num_points: int
        Number of points to use for the smooth interpolation
    color: str or tuple
        Color for the hull patch

    Returns:
    PathPatch: A matplotlib patch that can be added to an axis
    """
    # Convert points to numpy array if not already
    points = np.asarray(points)

    # Ensure we have enough points for a hull
    if len(points) < 3:
        raise ValueError("Need at least 3 points to create a hull")

    # Compute the convex hull
    hull = ConvexHull(points)
    hull_points = points[hull.vertices]

    # Close the hull by repeating the first point
    hull_points = np.vstack((hull_points, hull_points[0]))

    # Compute centroid for scaling
    centroid = np.mean(hull_points, axis=0)

    # Pre-scale the hull points before smoothing
    # This helps prevent the smoothing from shrinking the hull
    hull_points = centroid + (hull_points - centroid) * scale_factor

    # Create a periodic interpolation of the hull points
    try:
        tck, _u = splprep([hull_points[:, 0], hull_points[:, 1]],
                         s=smoothing, per=True, k=min(3, len(hull_points) - 1))
    except Exception:
        # Fallback to simpler smoothing if splprep fails
        tck, _u = splprep([hull_points[:, 0], hull_points[:, 1]],
                         s=smoothing * 2, per=True, k=min(2, len(hull_points) - 1))

    # Generate more points along the smooth curve
    new_points = np.linspace(0, 1, num_points)
    smooth_points = np.array(splev(new_points, tck)).T

    # Verify that all original points are inside the hull
    # If not, increase scale factor adaptively
    max_iterations = 100  # Safety limit to prevent infinite loop
    iteration = 0

    while iteration < max_iterations:
        path = Path(np.vstack((smooth_points, smooth_points[0])))
        if all(path.contains_points(points)):
            break

        # Increase scale factor by 5%
        smooth_points = centroid + (smooth_points - centroid) * 1.05
        iteration += 1

        if iteration == max_iterations:
            print(
                "Warning: Maximum iterations reached. Some points may not be fully enclosed.")

    # Create a matplotlib path for the smooth hull
    vertices = np.vstack((smooth_points, smooth_points[0]))  # Close the path
    codes = [Path.MOVETO] + [Path.LINETO] * \
        (len(vertices) - 2) + [Path.CLOSEPOLY]

    path = Path(vertices, codes)
    return PathPatch(path, fill=True, alpha=alpha, color=color)


def highlighted_plot(embedding, df, highlight_lines, colors=None, no_points=False, only_highlighted=False, annot_lines=None, backgrounds=False, save_path=None, debug=False):
    plt.clf()
    point_size = 400
    alpha = 0.5 if not only_highlighted else 0
    plt.scatter(embedding[:, 0], embedding[:, 1], s=point_size,
                alpha=alpha)
    for i, line in enumerate(highlight_lines):
        subset = embedding[df[line] == 1]
        if isinstance(colors, dict):
            color = colors[line]
        elif colors is None:
            color = None
        else:
            color = colors[i]
        if color and backgrounds:
            alpha = 0.4
            smooth_patch = create_smooth_scaled_hull(
                subset, scale_factor=1.3, smoothing=0.3, color=lighten_color(color), alpha=alpha)
            plt.gca().add_patch(smooth_patch)
        alpha = 0 if no_points else 1
        plt.scatter(subset[:, 0], subset[:, 1], s=point_size,
                    alpha=alpha, label=rename(line), color=color)

    if annot_lines:
        mask = np.array(df.loc[:, annot_lines] == 1).reshape(len(df))
        annot_subset = embedding[mask]
        annot_idx = df[mask].index
        for i, idx in enumerate(annot_idx):
            plt.text(annot_subset[i, 0], annot_subset[i,
                     1], df.index[idx]+1, fontsize=10)
            #  df["Name"][idx][:10], fontsize=10)
    if debug:
        plt.legend(bbox_to_anchor=(1.05, 1),
                   loc='upper left', borderaxespad=0)
    else:
        plt.gca().axes.get_xaxis().set_visible(False)
        plt.gca().axes.get_yaxis().set_visible(False)
    sns.despine(left=True, bottom=True)
    # plt.tight_layout()
    plt.xlim(-2.85, 3.1)
    plt.ylim(-3.1, 2.7)
    if save_path is not None:
        plt.savefig(save_path, transparent=True,
                    pad_inches=0, bbox_inches='tight')
        print(f"saved to {os.path.abspath(save_path)}")


def plot_dimensionality_reduction(df, focus_lines=None, projection="tsne", fig_dir="output"):
    if focus_lines is None:
        focus_lines = list(df.columns)
    distances = [  # 1, 3, 6, 10, 16,
        # 18,
        # 19,
        20
        # , 21
    ]
    neighbors = [2]
    # subplot_xy = (len(distances), len(neighbors))
    subplot_xy = max(1, int(len(distances)/2)), max(1,
                                                    int(len(distances)/2))

    # plt.subplots()
    plt.subplots(*subplot_xy, figsize=(9, 12))
    for di, distance in enumerate(distances):
        for ni, neighbor in enumerate(neighbors):
            plt.subplot(*subplot_xy,
                        di*len(neighbors) + ni + 1)
            os.makedirs(fig_dir, exist_ok=True)
            df_binary = df[all_lines].applymap(
                lambda x: 1 if not pd.isna(x) and x > 0 else 0)
            added, all_df_binary = add_cols(df_binary)
            reweighted_df = manual_reweight(all_df_binary)
            if projection.lower() == "tsne":
                focus_reweighted_df = reweighted_df[focus_lines + added]
                embedding = reduce_tsne(focus_reweighted_df)
            elif projection == "umap":
                focus_reweighted_df = reweighted_df[focus_lines + added]
                embedding = reduce_umap(focus_reweighted_df)
            else:
                projection = ""
                embedding = reduce_manually(
                    all_df_binary, neighbor, distance, focus=focus_lines + added)
                # raise NotImplementedError
            debug = False
            color_dict = {"The Science of Deep Learning": "#DF473A", commun_col: "#EFC344", "Recycling": "#E79E39", "Small Models": "#ECB740", collab_col: "#E18632", other_effi: "#8C7325",
                          efival_col: "#CF347A", "Evaluation": "#D73E5A", "Efficiency": "#8C7325", "Human-Model Interaction": "#5DB6E4", "Language&Cognition": "#60B4C2", "Debating": "#C2CC41", "Resources": "#43bccd", "Training": "#D27D2D"}
            # side
            highlight_lines = ["Recycling"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, no_points=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir,  "merging_moving to the side"))
            highlight_lines = ["Small Models"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, no_points=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir,  "smallm_moving to the side"))
            highlight_lines = ["Human-Model Interaction"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, no_points=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir,  "future_moving to the side"))
            highlight_lines = [efival_col]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, no_points=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir,  "efival_moving to the side"))
            highlight_lines = ["Recycling"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "merging_side"))
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, no_points=True, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "merging_sum_empty"))
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "merging_sum_points"))
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, only_highlighted=True, debug=True, annot_lines=highlight_lines, save_path=os.path.join(fig_dir, "merging_side_points"))
            highlight_lines = [efival_col]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "Efival_side"))
            highlight_lines = ["Small Models"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "smallm_side"))
            highlight_lines = [commun_col, "Small Models"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "smallm_side+"))
            highlight_lines = [commun_col, "Evaluation"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "Efival_end"))
            highlight_lines = ["Human-Model Interaction"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "future_side"))
            highlight_lines = ["Human-Model Interaction"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, backgrounds=True, debug=debug, save_path=os.path.join(fig_dir, "future_start"))
            highlight_lines = [commun_col, "Recycling"]
            subcolor_dict = {
                key: val if key != commun_col else None for key, val in color_dict.items()}
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, annot_lines="Recycling", colors=subcolor_dict, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "merging_end"))
            highlight_lines = [commun_col, "Recycling", "Small Models"]
            subcolor_dict = {
                key: val if key != commun_col else None for key, val in color_dict.items()}
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=subcolor_dict, backgrounds=True, only_highlighted=True, debug=debug, save_path=os.path.join(fig_dir, "Small Models"))
            # overview

            highlight_lines = [commun_col, "Recycling",
                               efival_col]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, only_highlighted=True, no_points=True, backgrounds=True, debug=debug, save_path=os.path.join(fig_dir, "overview"))
            highlight_lines = [commun_col]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, only_highlighted=True, no_points=True, backgrounds=True, debug=debug, save_path=os.path.join(fig_dir, "Communal_overview"))
            highlight_lines = [commun_col, "Recycling"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, only_highlighted=True, no_points=True, backgrounds=True, debug=debug, save_path=os.path.join(fig_dir, "overview_recycle"))
            highlight_lines = [commun_col, "Recycling",
                               efival_col]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, only_highlighted=True, no_points=True, backgrounds=True, debug=debug, save_path=os.path.join(fig_dir, "overview_efival"))
            highlight_lines = [commun_col, "Recycling",
                               efival_col, "Human-Model Interaction"]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, only_highlighted=True, no_points=True, backgrounds=True, debug=debug, save_path=os.path.join(fig_dir, "overview_future"))

            highlighted_plot(embedding, all_df_binary,
                             [], colors=color_dict, debug=debug, save_path=os.path.join(fig_dir, "empty"))
            # summary
            highlight_lines = [commun_col,
                               efival_col]
            highlighted_plot(embedding, all_df_binary,
                             highlight_lines, colors=color_dict, only_highlighted=True, backgrounds=True, debug=debug, save_path=os.path.join(fig_dir, "summary_efival"))
            for backgrounds in [True, False]:
                highlight_lines = [commun_col]
                highlighted_plot(embedding, all_df_binary,
                                 highlight_lines, colors=color_dict, backgrounds=backgrounds, debug=debug, save_path=os.path.join(fig_dir, f"Summary_access_{backgrounds}"))
                highlight_lines = ["Debating"]
                highlighted_plot(embedding, all_df_binary,
                                 highlight_lines, colors=color_dict, backgrounds=backgrounds, debug=debug, save_path=os.path.join(fig_dir, f"Summary_debate_{backgrounds}"))
                highlight_lines = ["Language&Cognition"]
                highlighted_plot(embedding, all_df_binary,
                                 highlight_lines, colors=color_dict, backgrounds=backgrounds, debug=debug, save_path=os.path.join(fig_dir, f"Summary_lang_{backgrounds}"))
                highlight_lines = ["The Science of Deep Learning"]
                highlighted_plot(embedding, all_df_binary,
                                 highlight_lines, colors=color_dict, backgrounds=backgrounds, debug=debug, save_path=os.path.join(fig_dir, f"Summary_understand_{backgrounds}"))
            plt.close()


# Smoothing factor (adjust this to control smoothness)
smoothing_factor = 5  # Higher values = more smoothing

# Read the XLS file

RENAME_DICT = {'The Science of Deep Learning': 'The Science of\nDeep Learning'}


def rename(line):
    return RENAME_DICT.get(line, line)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
df = pd.read_csv(os.path.join(ROOT, 'papers.csv'))
all_lines = ["NLP", "Small Models", "Debating", "Recycling", "Scaling Laws", "Human-Model Interaction", "Efficient Pretraining Research", "Resources", "The Science of Deep Learning",
             "Methods", "Dataset", "Training", "Evaluation", r"Shared-task\effort", "Language&Cognition", "Open", "Meta-science", "Enabling Low Budget Research", "Efficiency"]

lines = ["Small Models", "Recycling", "Efficient Pretraining Research",  # "The Science of Deep Learning"  # , "Training",
         "Evaluation", "Language&Cognition", "Meta-science", "Enabling Low Budget Research", "Efficiency"]
# lines = ['NLP', 'Enabling Low Budget Research', 'The Science of\nDeep Learning', 'Methods',
#          'Evaluation', 'Open', 'Language&Cognition',  'Resources'
#          ]
# df = df.rename(
#     columns=rename_df)
# lines = [rename_df.get(line, line) for line in lines]
# all_lines = [rename_df.get(line, line) for line in all_lines]
x = "Time of publish ID"
df = df.dropna(subset=[x])
df = df.sort_values(x)
# df_sums = df[lines].fillna(0).cumsum()
# df_averages = df[lines].fillna(0).expanding().mean()
plot_dimensionality_reduction(df, lines, "manual")
# plot_cooccurence(df[lines])
# df = read_df()
